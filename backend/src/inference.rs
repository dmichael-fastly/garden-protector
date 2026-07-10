//! On-edge image inference: model load + JPEG preprocessing + logit→decision.
//!
//! Pure compute (plus the one KV fetch of the model weights at boot). NONE of this is
//! a fail-closed branch: the `MODEL` cache (in main.rs) turns a load failure into
//! `None`, and `handle_evidence` fails CLOSED when the model is absent — that branch
//! stays in main.rs. Extracted verbatim in Phase 5b modularization — behavior
//! unchanged. The Tract/fastly types compile natively, so this links under both
//! wasm32 and native `cargo test`.

use crate::contract_gen::MITIGATE_THRESHOLD;
use crate::log_evt;
use fastly::{kv_store::KVStore, Error};
use std::io::Cursor;
use std::sync::Arc;
use std::time::SystemTime;
use tract_onnx::prelude::*;

/// Loads model weights from the Fastly KV Store and builds the Tract execution plan.
pub fn load_model_from_kv() -> Result<Arc<TypedRunnableModel>, Error> {
    // 1. Open the "garden_models" KV store
    let store = match KVStore::open("garden_models")? {
        Some(s) => s,
        None => return Err(Error::msg("KV Store link 'garden_models' not found")),
    };

    // 2. Fetch the ONNX model weights directly (no builder `.execute()` needed)
    let load_start = SystemTime::now();
    let model_entry = store
        .lookup("mobilenet_v2.onnx")?
        .ok_or_else(|| Error::msg("Model weights 'mobilenet_v2.onnx' not found in KV Store."))?;

    let model_bytes = model_entry.into_bytes();
    log_evt(
        "boot",
        "infer",
        "model_fetch",
        "ok",
        &format!("weights_bytes={}", model_bytes.len()),
    );
    let mut reader = Cursor::new(model_bytes);

    // 3. Parse and optimize the ONNX model using Tract
    let model = tract_onnx::onnx()
        .model_for_read(&mut reader)?
        .with_input_fact(0, f32::fact(&[1, 3, 224, 224]).into())?
        .into_optimized()?
        .into_runnable()?;
    let load_ms = load_start
        .elapsed()
        .map(|d| d.as_secs_f64() * 1000.0)
        .unwrap_or(0.0);
    log_evt(
        "boot",
        "infer",
        "model_load",
        "ok",
        &format!("optimized+runnable load_ms={:.2}", load_ms),
    );

    Ok(model)
}

/// Decodes and normalizes a JPEG byte array to a standard input tensor [1, 3, 224, 224] for MobileNet
pub fn preprocess_image(jpeg_bytes: &[u8]) -> Result<Tensor, Error> {
    // A. Decode JPEG bytes using pure Rust image crate
    let img = image::load_from_memory_with_format(jpeg_bytes, image::ImageFormat::Jpeg)
        .map_err(|e| Error::msg(format!("Failed to decode image: {}", e)))?;

    // B. Resize image to 224x224 (required by MobileNet V2 ONNX)
    let resized = img.resize_exact(224, 224, image::imageops::FilterType::Triangle);
    let rgb = resized.to_rgb8();

    // C. Convert pixel values to Float32 and normalize
    // Mean and StdDev values for MobileNet standard normalization
    let mean = [0.485, 0.456, 0.406];
    let std = [0.229, 0.224, 0.225];

    let mut tensor = tract_ndarray::Array4::<f32>::zeros((1, 3, 224, 224));

    for y in 0..224 {
        for x in 0..224 {
            let pixel = rgb.get_pixel(x, y);
            for c in 0..3 {
                // Normalize and write to tensor
                let val = (pixel[c] as f32 / 255.0 - mean[c]) / std[c];
                tensor[[0, c, y as usize, x as usize]] = val;
            }
        }
    }

    Ok(tensor.into())
}

// MITIGATE_THRESHOLD (min top-1 softmax probability to act) is generated -> contract_gen.

/// Curated allowlist of ImageNet-1k (ILSVRC2012) classes that count as garden
/// critters, mapped to a human-readable label.
///
/// NOTE: stock MobileNet V2 trained on ImageNet-1k has **no `raccoon` synset**,
/// so reliable raccoon/deer detection ultimately needs a fine-tuned model. This
/// list covers the recognizable mammals a garden camera is most likely to catch;
/// extend or replace it (and ship a full labels file) for production.
fn critter_label(class: usize) -> Option<&'static str> {
    match class {
        272 => Some("coyote"),
        277 => Some("red fox"),
        278 => Some("kit fox"),
        279 => Some("Arctic fox"),
        280 => Some("grey fox"),
        281 => Some("tabby cat"),
        282 => Some("tiger cat"),
        283 => Some("Persian cat"),
        284 => Some("Siamese cat"),
        285 => Some("Egyptian cat"),
        330 => Some("cottontail rabbit"),
        331 => Some("hare"),
        332 => Some("Angora rabbit"),
        334 => Some("porcupine"),
        335 => Some("fox squirrel"),
        336 => Some("marmot"),
        337 => Some("beaver"),
        338 => Some("guinea pig"),
        342 => Some("wild boar"),
        356 => Some("weasel"),
        357 => Some("mink"),
        358 => Some("polecat"),
        359 => Some("black-footed ferret"),
        360 => Some("otter"),
        361 => Some("skunk"),
        _ => None,
    }
}

/// Turns raw model logits into a `(species, confidence, action)` decision.
///
/// MobileNet V2 emits raw logits (no Softmax op in the graph), so we apply a
/// numerically-stable softmax (subtract the max logit before `exp`) to get a real
/// probability, then only `mitigate` when the top class is an allowlisted critter
/// AND the probability clears `MITIGATE_THRESHOLD`.
pub fn classify_logits(logits: &[f32]) -> (String, f32, &'static str) {
    let mut best_class = 0usize;
    let mut best_logit = f32::NEG_INFINITY;
    let mut max_logit = f32::NEG_INFINITY;
    for (i, &l) in logits.iter().enumerate() {
        if l > best_logit {
            best_logit = l;
            best_class = i;
        }
        if l > max_logit {
            max_logit = l;
        }
    }
    let sum_exp: f32 = logits.iter().map(|&l| (l - max_logit).exp()).sum();
    let confidence = if sum_exp > 0.0 {
        (best_logit - max_logit).exp() / sum_exp
    } else {
        0.0
    };

    let label = critter_label(best_class);
    let action = if label.is_some() && confidence >= MITIGATE_THRESHOLD {
        "mitigate"
    } else {
        "none"
    };
    let species = label
        .map(|s| s.to_string())
        .unwrap_or_else(|| format!("class_{}", best_class));
    (species, confidence, action)
}
