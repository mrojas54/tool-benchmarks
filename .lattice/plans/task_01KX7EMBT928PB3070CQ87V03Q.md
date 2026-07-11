# TB-26: session-grain cache-token sums for Claude (read + creation): promote the _is_cache_hit boolean to summed session fields

Populate session_cache_read_tokens for Claude sessions (currently hermes-only, transcript.py:91) and add session_cache_creation_tokens, both summed from per-message usage.cache_*_input_tokens (today read only as the _is_cache_hit boolean, passive.py:275). NULL-vs-measured semantics per S32; hermes path unchanged; date-range survival (TB-25) extends to the new field; Summary caveat line (read + creation). Spec: S39. Prerequisite for TB-27.
