//! Alarms — a *trigger* from a detector (radar / camera / other) becomes an alarm, gets a
//! human determination ("good/neutral/bad" = real/unsure/false), and feeds an advisory
//! per-species threshold recommendation. This module is the PURE, host-ABI-free core (links
//! under native `cargo test`): the alarm record + log, the id rule, the tag→histogram rollup,
//! the recommendation math, and the retention trims. The KV I/O (record/load/tag/trim) +
//! route handlers live in main.rs/routes.rs.
//!
//! Storage is ONE per-garden JSON doc `g/<gid>/alarm_log` mapping an alarm id -> the alarm, so
//! the whole set is listable/editable/retainable (Fastly KV has no prefix listing) and the
//! recommendation histograms are always recomputable from it.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// The three determination labels (real / unsure / false), stored verbatim. The Alarms UI
/// presents them as Real/Unsure/False.
pub const LABELS: [&str; 3] = ["good", "neutral", "bad"];

/// Minimum tagged alarms for a species before we offer a tuning recommendation. Edge-only.
pub const MIN_LABELS: u32 = 3;

/// Hard ceiling on the per-garden alarm log between cron sweeps (a belt to retention): when an
/// append would exceed this, the oldest are trimmed inline so the single KV doc can't grow
/// unbounded if a scheduled camera is (deliberately) made a trigger.
pub const ALARM_LOG_CAP: usize = 5000;

/// Is `label` one of the accepted determination labels?
pub fn label_valid(label: &str) -> bool {
    LABELS.contains(&label)
}

/// The alarm id for a capture: the capture BATCH when present (so a multi-angle set dedups to
/// ONE alarm), else the request trace id (`cid`). The batch slug "none"/"" means "no batch".
pub fn alarm_id(batch: &str, cid: &str) -> String {
    let b = batch.trim();
    if b.is_empty() || b == "none" {
        cid.to_string()
    } else {
        b.to_string()
    }
}

/// One stored alarm. `key` is the primary trigger frame's archive key (thumbnail + deep-link);
/// confirming camera angles are resolved at view time by `batch` (reuse the event page's
/// "other angles of this moment"). `tag` is the human determination, null until judged.
#[derive(Clone, Serialize, Deserialize, PartialEq, Debug)]
pub struct AlarmRecord {
    pub id: String,
    pub ts: u64,
    pub trigger_device: String,
    pub key: String,
    #[serde(default)]
    pub batch: String,
    pub species: String,
    pub confidence: u32,
    pub action: String,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub tag: Option<String>,
}

/// id -> alarm. Stored as the JSON value of `g/<gid>/alarm_log`. BTreeMap for deterministic
/// serialization; handlers re-sort the returned list by recency.
pub type AlarmLog = BTreeMap<String, AlarmRecord>;

/// Per-label confidence histogram for ONE species: 10 buckets of 10%-wide confidence
/// (bucket i = [i*10, i*10+10)% ; 100% lands in bucket 9).
#[derive(Default, Clone, Serialize, Deserialize, PartialEq, Debug)]
pub struct SpeciesHist {
    pub good: [u32; 10],
    pub neutral: [u32; 10],
    pub bad: [u32; 10],
}

/// species -> histogram, derived from the TAGGED alarms in the log.
pub type AlarmStats = BTreeMap<String, SpeciesHist>;

/// The bucket (0..=9) a 0..=100 confidence percent falls in.
pub fn bucket_of(conf_pct: u32) -> usize {
    ((conf_pct / 10) as usize).min(9)
}

/// Build per-species confidence histograms from the TAGGED alarms (untagged alarms contribute
/// nothing — they carry no determination yet). Derived fresh on each read.
pub fn stats_from_alarms(log: &AlarmLog) -> AlarmStats {
    let mut stats = AlarmStats::new();
    for r in log.values() {
        let tag = match &r.tag {
            Some(t) => t.as_str(),
            None => continue,
        };
        let h = stats.entry(r.species.clone()).or_default();
        let b = bucket_of(r.confidence);
        match tag {
            "good" => h.good[b] += 1,
            "neutral" => h.neutral[b] += 1,
            "bad" => h.bad[b] += 1,
            _ => {}
        }
    }
    stats
}

/// Drop alarms older than `keep_days` (relative to `now_ms`). Returns how many were removed.
pub fn prune_by_days(log: &mut AlarmLog, now_ms: u64, keep_days: u32) -> usize {
    let cutoff = now_ms.saturating_sub((keep_days as u64) * 86_400_000);
    let before = log.len();
    log.retain(|_, r| r.ts >= cutoff);
    before - log.len()
}

/// Keep only the newest `keep_count` alarms (by `ts`); drop the rest. Returns how many removed.
pub fn prune_by_count(log: &mut AlarmLog, keep_count: usize) -> usize {
    if log.len() <= keep_count {
        return 0;
    }
    let mut by_ts: Vec<(String, u64)> = log.iter().map(|(id, r)| (id.clone(), r.ts)).collect();
    by_ts.sort_by(|a, b| b.1.cmp(&a.1)); // newest first
    let drop_ids: Vec<String> = by_ts
        .into_iter()
        .skip(keep_count)
        .map(|(id, _)| id)
        .collect();
    let n = drop_ids.len();
    for id in drop_ids {
        log.remove(&id);
    }
    n
}

/// An advisory per-species threshold recommendation, surfaced on the Alarms page.
#[derive(Serialize, PartialEq, Debug)]
pub struct SpeciesRecommendation {
    pub species: String,
    pub good: u32,
    pub neutral: u32,
    pub bad: u32,
    /// Suggested confidence gate (%), or `None` when no change is advised.
    pub recommended_pct: Option<u32>,
    /// Plain-English rationale for a human.
    pub note: String,
}

fn sum(a: &[u32; 10]) -> u32 {
    a.iter().sum()
}
fn highest_nonzero(a: &[u32; 10]) -> Option<usize> {
    (0..10).rev().find(|&i| a[i] > 0)
}
fn lowest_nonzero(a: &[u32; 10]) -> Option<usize> {
    (0..10).find(|&i| a[i] > 0)
}

/// Advisory per-species threshold recommendation from the tag histogram. `current_pct` is the
/// live global gate (round(MITIGATE_THRESHOLD*100)). Direction, in priority order:
///   * RAISE — "bad" (false-alarm) tags reaching AT/ABOVE the gate -> raise just above the worst.
///   * LOWER — else "good" (confirmed) tags BELOW the gate with NO false alarms in the gap -> lower.
///   * HOLD  — otherwise (well-tuned, or < `min_labels` tags). Pure + unit-tested.
pub fn recommend(
    species: &str,
    h: &SpeciesHist,
    current_pct: u32,
    min_labels: u32,
) -> SpeciesRecommendation {
    let (ng, nn, nb) = (sum(&h.good), sum(&h.neutral), sum(&h.bad));
    let total = ng + nn + nb;
    let mk = |rec: Option<u32>, note: String| SpeciesRecommendation {
        species: species.to_string(),
        good: ng,
        neutral: nn,
        bad: nb,
        recommended_pct: rec,
        note,
    };
    if total < min_labels {
        let need = min_labels - total;
        return mk(
            None,
            format!(
                "Tag {} more alarm{} and we'll suggest a tuning.",
                need,
                if need == 1 { "" } else { "s" }
            ),
        );
    }
    let cur_b = bucket_of(current_pct);
    if let Some(hb) = highest_nonzero(&h.bad) {
        let cand = (((hb + 1) * 10) as u32).min(100);
        if cand > current_pct {
            return mk(
                Some(cand),
                format!(
                    "You flagged {} false alarm{} up to ~{}% confidence. Raising the {} threshold to ~{}% would skip those while keeping the {} you confirmed.",
                    nb,
                    if nb == 1 { "" } else { "s" },
                    (((hb * 10) + 10) as u32).min(100),
                    species,
                    cand,
                    ng
                ),
            );
        }
    }
    if let Some(lg) = lowest_nonzero(&h.good) {
        let cand = (lg * 10) as u32;
        if cand < current_pct {
            let bad_in_gap: u32 = (lg..cur_b).map(|i| h.bad[i]).sum();
            if bad_in_gap == 0 {
                return mk(
                    Some(cand),
                    format!(
                        "You confirmed {} real alarm{} as low as ~{}% confidence. Lowering the {} threshold to ~{}% would catch similar ones.",
                        ng,
                        if ng == 1 { "" } else { "s" },
                        cand,
                        species,
                        cand
                    ),
                );
            }
        }
    }
    mk(
        None,
        format!(
            "Detection looks well-tuned — {} confirmed, {} false alarm{}.",
            ng,
            nb,
            if nb == 1 { "" } else { "s" }
        ),
    )
}

/// All per-species recommendations (sorted by species — BTreeMap order — for stable output).
pub fn recommendations(
    stats: &AlarmStats,
    current_pct: u32,
    min_labels: u32,
) -> Vec<SpeciesRecommendation> {
    stats
        .iter()
        .map(|(sp, h)| recommend(sp, h, current_pct, min_labels))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn alarm(id: &str, tag: Option<&str>, species: &str, conf: u32, ts: u64) -> AlarmRecord {
        AlarmRecord {
            id: id.into(),
            ts,
            trigger_device: "cam".into(),
            key: format!("g/yard/evidence/2026/06/24/{}.jpg", id),
            batch: String::new(),
            species: species.into(),
            confidence: conf,
            action: "none".into(),
            reason: None,
            tag: tag.map(String::from),
        }
    }

    #[test]
    fn test_label_valid() {
        assert!(label_valid("good") && label_valid("neutral") && label_valid("bad"));
        assert!(!label_valid("") && !label_valid("Good") && !label_valid("real"));
    }

    #[test]
    fn test_alarm_id_prefers_batch_else_cid() {
        assert_eq!(alarm_id("batch-1", "cid-x"), "batch-1");
        assert_eq!(alarm_id("", "cid-x"), "cid-x"); // no batch -> cid
        assert_eq!(alarm_id("none", "cid-x"), "cid-x"); // slug "none" -> cid
        assert_eq!(alarm_id("  ", "cid-x"), "cid-x"); // blank -> cid
    }

    #[test]
    fn test_bucket_of_clamps_to_nine() {
        assert_eq!(bucket_of(0), 0);
        assert_eq!(bucket_of(35), 3);
        assert_eq!(bucket_of(100), 9); // must not overflow to bucket 10
    }

    #[test]
    fn test_stats_only_counts_tagged_alarms() {
        let mut log = AlarmLog::new();
        log.insert("a".into(), alarm("a", Some("good"), "red-fox", 62, 1));
        log.insert("b".into(), alarm("b", Some("bad"), "red-fox", 41, 2));
        log.insert("c".into(), alarm("c", None, "red-fox", 70, 3)); // untagged -> ignored
        log.insert("d".into(), alarm("d", Some("good"), "red-fox", 65, 4)); // 60s bucket like a
        let stats = stats_from_alarms(&log);
        let fox = stats.get("red-fox").expect("fox present");
        assert_eq!(fox.good[6], 2, "two good fox tags in the 60% bucket");
        assert_eq!(fox.bad[4], 1);
        // The untagged 70% alarm must NOT appear in any bucket.
        assert_eq!(fox.good[7], 0);
    }

    #[test]
    fn test_prune_by_days_drops_old() {
        let mut log = AlarmLog::new();
        let now = 100 * 86_400_000u64; // day 100
        log.insert("old".into(), alarm("old", None, "x", 0, 10 * 86_400_000)); // day 10
        log.insert("new".into(), alarm("new", None, "x", 0, 99 * 86_400_000)); // day 99
        let removed = prune_by_days(&mut log, now, 7); // keep last 7 days
        assert_eq!(removed, 1);
        assert!(log.contains_key("new") && !log.contains_key("old"));
    }

    #[test]
    fn test_prune_by_count_keeps_newest() {
        let mut log = AlarmLog::new();
        for i in 0..5u64 {
            log.insert(
                format!("a{}", i),
                alarm(&format!("a{}", i), None, "x", 0, i),
            );
        }
        let removed = prune_by_count(&mut log, 2); // keep newest 2 (ts 4,3)
        assert_eq!(removed, 3);
        assert!(log.contains_key("a4") && log.contains_key("a3"));
        assert!(!log.contains_key("a0"));
    }

    #[test]
    fn test_recommend_needs_min_labels() {
        let mut h = SpeciesHist::default();
        h.good[6] = 2;
        let r = recommend("red-fox", &h, 30, MIN_LABELS);
        assert_eq!(r.recommended_pct, None);
        assert!(r.note.contains("1 more"));
    }

    #[test]
    fn test_recommend_raise_to_cut_false_alarms() {
        let mut h = SpeciesHist::default();
        h.good[7] = 5;
        h.bad[5] = 3; // false alarms at 50-60%
        let r = recommend("red-fox", &h, 30, MIN_LABELS);
        assert_eq!(r.recommended_pct, Some(60));
        assert!(r.note.contains("false alarm"));
    }

    #[test]
    fn test_recommend_lower_when_gap_is_clean() {
        let mut h = SpeciesHist::default();
        h.good[1] = 2; // confirmed at 10-20%
        h.good[4] = 2;
        let r = recommend("rabbit", &h, 30, MIN_LABELS);
        assert_eq!(r.recommended_pct, Some(10));
        assert!(r.note.contains("Lowering"));
    }

    #[test]
    fn test_recommend_hold_when_well_tuned() {
        let mut h = SpeciesHist::default();
        h.good[5] = 4;
        let r = recommend("red-fox", &h, 30, MIN_LABELS);
        assert_eq!(r.recommended_pct, None);
        assert!(r.note.contains("well-tuned"));
    }

    #[test]
    fn test_record_roundtrips_json_with_defaults() {
        let r = alarm("x", Some("bad"), "red-fox", 41, 7);
        let s = serde_json::to_string(&r).unwrap();
        let back: AlarmRecord = serde_json::from_str(&s).unwrap();
        assert_eq!(r, back);
        // Missing optional fields (older record) default cleanly.
        let minimal = r#"{"id":"y","ts":1,"trigger_device":"cam","key":"k","species":"x","confidence":5,"action":"none"}"#;
        let m: AlarmRecord = serde_json::from_str(minimal).unwrap();
        assert_eq!(m.tag, None);
        assert_eq!(m.batch, "");
        assert_eq!(m.reason, None);
    }
}
