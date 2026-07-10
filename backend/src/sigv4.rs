//! AWS SigV4 request signing + the RFC3986/canonical-query encoding it needs, for the
//! best-effort evidence-archive reads/writes to Fastly Object Storage (off the
//! fail-closed safety path). PURE — only hashing/HMAC/encoding + UTC date formatting,
//! no host-ABI — so it links under both wasm32 and native `cargo test`. Extracted
//! verbatim from main.rs (Phase 5 modularization; behavior unchanged).

use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};
use time::OffsetDateTime;

pub fn hex_lower(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

pub fn sha256_hex(data: &[u8]) -> String {
    hex_lower(&Sha256::digest(data))
}

pub fn hmac_sha256(key: &[u8], data: &[u8]) -> Vec<u8> {
    let mut mac = Hmac::<Sha256>::new_from_slice(key).expect("HMAC accepts any key length");
    mac.update(data);
    mac.finalize().into_bytes().to_vec()
}

/// Builds an AWS SigV4 `Authorization` header for an S3-style request. Pure +
/// testable against the published AWS SigV4 test vectors. Signs exactly the
/// `host;x-amz-content-sha256;x-amz-date` header set (what FOS expects).
#[allow(clippy::too_many_arguments)]
pub fn sigv4_authorization(
    method: &str,
    host: &str,
    canonical_uri: &str,
    canonical_query: &str,
    payload_hash_hex: &str,
    access_key: &str,
    secret_key: &str,
    region: &str,
    service: &str,
    amz_date: &str,   // YYYYMMDDTHHMMSSZ
    date_stamp: &str, // YYYYMMDD
) -> String {
    let signed_headers = "host;x-amz-content-sha256;x-amz-date";
    let canonical_headers = format!(
        "host:{}\nx-amz-content-sha256:{}\nx-amz-date:{}\n",
        host, payload_hash_hex, amz_date
    );
    let canonical_request = format!(
        "{}\n{}\n{}\n{}\n{}\n{}",
        method, canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash_hex
    );
    let scope = format!("{}/{}/{}/aws4_request", date_stamp, region, service);
    let string_to_sign = format!(
        "AWS4-HMAC-SHA256\n{}\n{}\n{}",
        amz_date,
        scope,
        sha256_hex(canonical_request.as_bytes())
    );
    let k_date = hmac_sha256(
        format!("AWS4{}", secret_key).as_bytes(),
        date_stamp.as_bytes(),
    );
    let k_region = hmac_sha256(&k_date, region.as_bytes());
    let k_service = hmac_sha256(&k_region, service.as_bytes());
    let k_signing = hmac_sha256(&k_service, b"aws4_request");
    let signature = hex_lower(&hmac_sha256(&k_signing, string_to_sign.as_bytes()));
    format!(
        "AWS4-HMAC-SHA256 Credential={}/{}, SignedHeaders={}, Signature={}",
        access_key, scope, signed_headers, signature
    )
}

/// `(amz_date YYYYMMDDTHHMMSSZ, date_stamp YYYYMMDD)` in UTC for SigV4 signing.
pub fn amz_times(epoch_secs: i64) -> (String, String) {
    let dt = OffsetDateTime::from_unix_timestamp(epoch_secs).unwrap_or(OffsetDateTime::UNIX_EPOCH);
    (
        format!(
            "{:04}{:02}{:02}T{:02}{:02}{:02}Z",
            dt.year(),
            u8::from(dt.month()),
            dt.day(),
            dt.hour(),
            dt.minute(),
            dt.second()
        ),
        format!("{:04}{:02}{:02}", dt.year(), u8::from(dt.month()), dt.day()),
    )
}

/// RFC3986 percent-encoding for SigV4 canonical query parts (encode everything
/// except the unreserved set `A-Za-z0-9-_.~`, so `/` becomes `%2F`). PURE.
pub fn uri_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len() * 3);
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{:02X}", b)),
        }
    }
    out
}

/// AWS canonical query string: each name/value uri-encoded, pairs sorted by encoded
/// name. PURE. (The same string is signed AND sent, so signature == request.)
pub fn canonical_query(params: &[(&str, String)]) -> String {
    let mut enc: Vec<(String, String)> = params
        .iter()
        .map(|(k, v)| (uri_encode(k), uri_encode(v)))
        .collect();
    enc.sort();
    enc.iter()
        .map(|(k, v)| format!("{}={}", k, v))
        .collect::<Vec<_>>()
        .join("&")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_uri_encode_and_canonical_query() {
        assert_eq!(uri_encode("g/abc/2026/"), "g%2Fabc%2F2026%2F");
        assert_eq!(uri_encode("a-b_c.d~e"), "a-b_c.d~e"); // unreserved untouched
                                                          // Sorted by encoded name; '/' in the prefix value is %2F.
        assert_eq!(
            canonical_query(&[
                ("prefix", "g/x/".into()),
                ("list-type", "2".into()),
                ("delimiter", "/".into())
            ]),
            "delimiter=%2F&list-type=2&prefix=g%2Fx%2F"
        );
        // max-keys (the newest-N day read) is part of the signed+sent canonical query,
        // sorted by encoded name — so the signature matches the wire URL.
        assert_eq!(
            canonical_query(&[
                ("list-type", "2".into()),
                ("prefix", "g/x/".into()),
                ("max-keys", "12".into())
            ]),
            "list-type=2&max-keys=12&prefix=g%2Fx%2F"
        );
    }

    #[test]
    fn test_sigv4_authorization_matches_known_vector() {
        // Cross-validated against Python hmac/hashlib for the exact 3-header
        // (host;x-amz-content-sha256;x-amz-date) canonical form this signer uses,
        // with the canonical AWS example credentials/date.
        let empty_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
        assert_eq!(sha256_hex(b""), empty_sha, "empty-payload sha256");
        let auth = sigv4_authorization(
            "GET",
            "examplebucket.s3.amazonaws.com",
            "/test.txt",
            "",
            empty_sha,
            "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            "us-east-1",
            "s3",
            "20130524T000000Z",
            "20130524",
        );
        assert!(
            auth.ends_with(
                "Signature=14f6a0997b2b70a86f4726658a6575b5109092ccb5fd328f51b369c44b4ac958"
            ),
            "got {}",
            auth
        );
        assert!(auth.contains("Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request"));
        assert!(auth.contains("SignedHeaders=host;x-amz-content-sha256;x-amz-date"));
    }

    #[test]
    fn test_hmac_and_hex_helpers() {
        // RFC 4231 / known HMAC-SHA256: key="key", data="The quick brown fox jumps over the lazy dog"
        let mac = hmac_sha256(b"key", b"The quick brown fox jumps over the lazy dog");
        assert_eq!(
            hex_lower(&mac),
            "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
        );
    }
}
