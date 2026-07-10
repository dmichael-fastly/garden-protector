"""gp-provision — Fastly Garden Protector control plane.

The SINGLE writer of the registry index docs (`index/gardens`,
`index/g/<gid>/devices`) and the minter of per-garden auth tokens, plus the
one-token deployment provisioner (Compute service + KV/Secret stores + Fastly
Object Storage bucket + CDN read-signing service).

Why a control plane (and why Python): Fastly KV in Compute has no CAS, so a
concurrent read-modify-write of the registry would lose updates. This process is
the single writer; the Pi never self-registers — it only carries the ids + token
this tool assigns it (RFC §4).
"""

__version__ = "1.0.0"
