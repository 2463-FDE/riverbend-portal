"""eligibility-service configuration. Environment-driven; sensible compose defaults.

The timeout and breaker numbers below are DERIVED, not guessed, and the
derivation is in adr/0008. Summary, because whoever changes them next should see
the reasoning before the value:

  * total timeout 5s   The portal's healthy /intake p95 is ~600ms and a
                       receptionist is standing in front of a patient. A check
                       slower than a few seconds has already failed them, so the
                       budget comes from the USER's tolerance, not the payer SLA.
  * threshold 5        Fewer trips on a single blip; more keeps feeding requests
                       into a dead endpoint. At five the breaker opens within
                       seconds of an incident.
  * cooldown 30s       The observed Tuesday outage was 19 minutes: ~38 probes,
                       cheap, and recovery within 30s of the payer returning.
  * cache TTL 24h      Coverage rarely changes intra-day. Long enough to cover a
                       full-day outage, short enough that a stale answer is
                       same-day.
"""
import os


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


class Settings:
    service_name = "eligibility-service"
    port = _i("PORT", 8072)
    environment = os.getenv("ENVIRONMENT", "development")
    log_level = os.getenv("LOG_LEVEL", "INFO")

    # Clearinghouse / payer REST shim that fronts the X12 270/271 exchange.
    payer_api_url = os.getenv("PAYER_API_URL", "https://edi.example.com/v1/eligibility")
    payer_api_key = os.getenv("PAYER_API_KEY", "")
    payer_name = os.getenv("PAYER_NAME", "edi.example.com")

    # --- W3: the resilience envelope (adr/0008) --------------------------- #
    payer_connect_timeout_s = _f("PAYER_CONNECT_TIMEOUT_S", 2.0)
    payer_read_timeout_s = _f("PAYER_READ_TIMEOUT_S", 4.0)
    payer_total_timeout_s = _f("PAYER_TOTAL_TIMEOUT_S", 5.0)

    breaker_failure_threshold = _i("PAYER_BREAKER_THRESHOLD", 5)
    breaker_cooldown_s = _f("PAYER_BREAKER_COOLDOWN_S", 30.0)

    eligibility_cache_ttl_s = _f("ELIGIBILITY_CACHE_TTL_S", 86400.0)


settings = Settings()
