# Active B3-SW re-audit handoff

Re-audit replaces the failed handoff at 19b582f5adbcf64e8fb49b560314345f9e06cfdb. The exact candidate is pinned by SHA256SUMS. The private source archive is the reproducible source of baseline and combined overlay, including optical assets. See CURSOR-FINDINGS-RESPONSE.md for H1–H5, M1–M8 and L1–L6 and remaining risks. See PROJECT_CONTEXT.UPDATE.md for proposed factual documentation corrections. Candidate remains offline.

Review the raw PTY/IPC test path, pidfd supervisor timing, lock hold duration, proof provenance, early-reject firmware path, rollback interlocks and physical marker measurement before authorizing deployment. All stop thresholds are proposals, and external marker/physical changes cannot be serialized by an in-process mutex. Save generated trace separately from collector and trigger evidence. Never infer idle from a failed or missing capture.
