# Finding W1-2 — The zero-retention preflight could never have passed

- **Severity:** High. The control was inoperable, and it failed *closed*, so the
  W1 summariser could not have been enabled with a real key.
- **Status:** Fixed. Verified live against Bedrock on 2026-07-30.
- **Found by:** the first live run with a real credential — L0, the test written
  months earlier for exactly this moment, which spends nothing.

---

## What happened

`retention.check()` refused every model:

```
model 'us.anthropic.claude-3-5-haiku-20241022-v1:0' failed the zero-retention
preflight: retention probe failed (ValidationException); failing closed.
```

Read as a policy refusal. It was not. The probe called the wrong API.

```python
client = boto3.client("bedrock", ...)
resp = client.get_foundation_model(modelIdentifier=model_id)
retention = resp["modelDetails"].get("dataRetention", {})   # <- never exists
```

`FoundationModelDetails` contains exactly `customizationsSupported`,
`inferenceTypesSupported`, `inputModalities`, `modelArn`, `modelId`,
`modelLifecycle`, `modelName`, `outputModalities`, `providerName` and
`responseStreamingSupported` — and nothing about retention
([API reference](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_GetFoundationModel.html)).
Confirmed against a live account:

```
modelDetails keys: ['customizationsSupported', 'inferenceTypesSupported',
 'inputModalities', 'modelArn', 'modelId', 'modelLifecycle', 'modelName',
 'outputModalities', 'providerName', 'responseStreamingSupported']
dataRetention  : << ABSENT >>
```

So `.get("dataRetention", {})` returned `{}`, `effective_mode` was `None`, and
`None` is not in `_SAFE_MODES`. The control refused everything, forever.

**The concept was researched correctly.** The module docstring describes
`none` / `default` / `provider_data_share` / `inherit` and the
project → account → model-default resolution order accurately. Only the call was
wrong.

## Two id namespaces, and nobody noticed they were different

`GetFoundationModel` also rejects an inference-profile id outright:

```
us.anthropic.claude-3-5-haiku-20241022-v1:0
  -> ValidationException: The provided model identifier is invalid.
```

Invocation uses a region-scoped **inference profile**; the retention API knows
only **foundation-model** ids. Two namespaces met at this function and the
mismatch was invisible because the function was never called.

## Where the retention posture actually lives

A different service — `bedrock-mantle` — returns precisely the shape the
docstring describes
([data retention](https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html)):

```
GET https://bedrock-mantle.{region}.api.aws/v1/models/{model}

{ "status": "available",
  "data_retention": { "mode": "default", "source": "account",
                      "allowed_modes": ["none", "default", "provider_data_share"] } }
```

Account scope is on the control plane:

```
GET https://bedrock.{region}.amazonaws.com/data-retention   ->  {"mode":"inherit"}
```

## Why it went unnoticed for the whole engagement

`check()` takes an **injectable probe** so tests run offline. Every test injected
one. `_default_probe` — the only code that touched AWS — had **zero coverage**,
by construction.

That seam is the same shape as three earlier findings in this engagement:
the corpus path that resolved only outside a container, the gateway route no test
posted through, the sensitivity flag nothing in production ever set. **The
abstraction that makes tests hermetic is also the abstraction that hides whether
the real implementation works.**

The mitigation was already in place and it worked: L0 exists precisely so the
first real credential meets a written test rather than a scramble, and it spends
nothing so it can gate everything below it. It found this in one run.

## Also found: the pinned model is end-of-life

```
anthropic.claude-3-5-haiku-20241022-v1:0
  -> ResourceNotFoundException: This model version has reached the end of its life.
```

Every current Anthropic model on Bedrock is `INFERENCE_PROFILE`-only, so a bare
model id fails at *call* time — in a demo, not in CI. `BEDROCK_MODEL_ID` now
pins `us.anthropic.claude-haiku-4-5-20251001-v1:0`, and L1 exists to catch the
next expiry.

## The fix

1. `_default_probe` calls `bedrock-mantle`'s `GET /v1/models/{id}`.
2. `retention_model_id()` maps profile id → model id, as a named function with
   its own parametrised test, because it is a namespace boundary.
3. Probe failures report the **message**, not just the exception type.
   `ValidationException` alone is true and useless; the text said
   *"The provided model identifier is invalid"* and would have pointed straight
   at the bug. It carries no prompt text — the call sends a model id and nothing
   else.
4. A model reported `status: unavailable` is surfaced as a refusal.

## What the live run then reported, honestly

```
effective_mode : default
allowed_modes  : ['none', 'default', 'provider_data_share']
account mode   : inherit
```

The model **supports** zero data retention. The account does not **request** it:
`inherit` falls through to the model default, `default`. Under `default` AWS may
retain for abuse detection and **the model provider does not receive the data**.

Turning that into real ZDR is one account-wide call, and it is deliberately not
ours to make:

```bash
curl -X PUT https://bedrock.$AWS_REGION.amazonaws.com/data-retention \
  -H "Authorization: Bearer $AWS_BEARER_TOKEN_BEDROCK" -d '{"mode":"none"}'
```

**Stated plainly for the client:** the smoke run below was executed with
`BEDROCK_REQUIRE_ZERO_RETENTION=false`, against synthetic seed patients, on their
instruction. It proves the plumbing. **It does not prove the ZDR control
end-to-end** — that remains unproven until the account is set to `none`, and it
is on the debt register as such rather than counted as done.
