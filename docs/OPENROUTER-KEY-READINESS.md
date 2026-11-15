# OpenRouter credential readiness

Spectra checks an OpenRouter credential with the provider's authenticated
`GET https://openrouter.ai/api/v1/key` endpoint. This does not submit a chat
completion, select a model, or spend inference credits.

- HTTP 200: the configured key is verified.
- HTTP 401 or 403: the provider rejected the key; the model picker marks
  OpenRouter unavailable and asks the operator to replace the key.
- Network errors, HTTP 429, and provider/server errors: readiness is
  `UNVERIFIED`, not `INVALID`. The model picker remains available because a
  temporary network problem is not proof that the credential is wrong.
- Results are cached for 60 seconds by a one-way SHA-256 fingerprint of the key.
  The key itself is never returned, logged, or included in diagnostics.

The dashboard's OpenRouter picker is disabled only when readiness is explicitly
`MISSING` or `INVALID`. If the probe cannot run, the row remains offered rather
than falsely reporting that the provider is unavailable.

This validation is separate from Condor's web-session JWT and Hummingbot API
authentication. Those are different trust domains and must be diagnosed
independently.
