package modelrouter.app

import kotlinx.serialization.Serializable
import kotlinx.serialization.SerialName
import kotlinx.serialization.json.Json

/**
 * Contract v1 (docs/CONTRACT.md). This app is a thin client: every type here mirrors a
 * shape the router actually returns. Nothing is computed or re-derived that the router
 * did not already decide — this client renders, it does not route.
 */
val RouterJson = Json {
    ignoreUnknownKeys = true
    isLenient = true
    coerceInputValues = true
    explicitNulls = false
    encodeDefaults = true
}

// ---- /health --------------------------------------------------------------------------------

@Serializable
data class Health(
    val status: String = "not_ready",
    val service: String = "",
    val version: String = "",
    @SerialName("providers_with_keys") val providersWithKeys: List<String> = emptyList(),
    val problems: List<String> = emptyList(),
)

// ---- /router/setup ----------------------------------------------------------------------------

@Serializable
data class KeyInfo(
    val provider: String = "",
    val source: String = "",
    val name: String = "",
    val present: Boolean = false,
    val detail: String = "",
)

@Serializable
data class Setup(
    val config: String = "",
    @SerialName("config_problems") val configProblems: List<String> = emptyList(),
    @SerialName("secrets_source") val secretsSource: String = "",
    @SerialName("gcp_project") val gcpProject: String? = null,
    @SerialName("gcloud_installed") val gcloudInstalled: Boolean = false,
    val keys: List<KeyInfo> = emptyList(),
    val auth: String = "",
    val dashboard: String = "",
    @SerialName("dashboard_status") val dashboardStatus: Map<String, String> = emptyMap(),
    val ready: Boolean = false,
)

// ---- shared: providers, seats -----------------------------------------------------------------

@Serializable
data class ProviderState(
    val provider: String = "",
    val key: String = "",
    val state: String = "",
    val detail: String = "",
    val until: Double? = null,
    val models: Int? = null,
    @SerialName("price_source") val priceSource: String = "",
    val ratelimit: Map<String, String> = emptyMap(),
    @SerialName("free_quota_until") val freeQuotaUntil: Double? = null,
) {
    val ok: Boolean get() = state.equals("OK", ignoreCase = true)
}

@Serializable
data class Candidate(
    val seat: String = "",
    val provider: String = "",
    val model: String = "",
    @SerialName("provider_state") val providerState: String = "",
    @SerialName("provider_detail") val providerDetail: String = "",
    val available: Boolean = false,
    @SerialName("context_length") val contextLength: Long? = null,
    @SerialName("supports_tools") val supportsTools: Boolean? = null,
    @SerialName("list_prompt") val listPrompt: Double? = null,
    @SerialName("list_completion") val listCompletion: Double? = null,
    @SerialName("price_source") val priceSource: String = "",
    @SerialName("measured_usd_per_mtok") val measuredUsdPerMtok: Double? = null,
    val emits: String? = null,
    @SerialName("min_max_tokens") val minMaxTokens: Int? = null,
    @SerialName("reasoning_overhead_tokens") val reasoningOverheadTokens: Int? = null,
    @SerialName("probe_age_s") val probeAgeS: Double? = null,
    @SerialName("latency_s") val latencyS: Double? = null,
)

// ---- /router/roster ---------------------------------------------------------------------------

@Serializable
data class RosterResponse(
    val providers: List<ProviderState> = emptyList(),
    val dashboard: Map<String, String> = emptyMap(),
    val seats: List<Candidate> = emptyList(),
)

// ---- Decision / Choice / Assessment / Attempt --------------------------------------------------

@Serializable
data class Ask(
    @SerialName("prompt_tokens") val promptTokens: Int? = null,
    @SerialName("max_tokens") val maxTokens: Int? = null,
    @SerialName("needs_tools") val needsTools: Boolean = false,
    @SerialName("ceiling_usd_per_mtok") val ceilingUsdPerMtok: Double? = null,
    val stream: Boolean = false,
)

@Serializable
data class Facts(
    @SerialName("prompt_tokens") val promptTokens: Int? = null,
    @SerialName("max_tokens") val maxTokens: Int? = null,
    @SerialName("needs_tools") val needsTools: Boolean = false,
    @SerialName("ceiling_usd_per_mtok") val ceilingUsdPerMtok: Double? = null,
    @SerialName("only_seat") val onlySeat: String? = null,
    @SerialName("roster_size") val rosterSize: Int? = null,
    @SerialName("unknown_not_listed") val unknownNotListed: Int? = null,
    @SerialName("free_tier_not_listed") val freeTierNotListed: Int? = null,
)

@Serializable
data class Assessment(
    val seat: String = "",
    val verdict: String = "UNKNOWN",
    val because: String = "",
    val unknown: List<String> = emptyList(),
    @SerialName("usd_per_mtok") val usdPerMtok: Double? = null,
    @SerialName("price_basis") val priceBasis: String = "",
    @SerialName("expected_usd") val expectedUsd: Double? = null,
)

@Serializable
data class Choice(
    val outcome: String = "ABSTAIN",
    val because: String = "",
    val seat: String? = null,
    @SerialName("max_tokens") val maxTokens: Int? = null,
    @SerialName("expected_usd") val expectedUsd: Double? = null,
    val unknown: List<String> = emptyList(),
    val facts: Facts = Facts(),
    val considered: List<Assessment> = emptyList(),
) {
    val isRoute: Boolean get() = outcome.equals("ROUTE", ignoreCase = true)
    val qualifying: List<Assessment> get() = considered.filter { it.verdict.equals("QUALIFIES", true) }
    val excluded: List<Assessment> get() = considered.filter { it.verdict.equals("EXCLUDED", true) }
    val unknownSeats: List<Assessment> get() = considered.filter { it.verdict.equals("UNKNOWN", true) }
}

@Serializable
data class Attempt(
    val seat: String = "",
    @SerialName("max_tokens") val maxTokens: Int? = null,
    val http: Int? = null,
    val ok: Boolean = false,
    val detail: String = "",
    @SerialName("finish_reason") val finishReason: String? = null,
    @SerialName("content_chars") val contentChars: Int = 0,
    @SerialName("reasoning_chars") val reasoningChars: Int = 0,
    @SerialName("tool_calls") val toolCalls: Int = 0,
    @SerialName("prompt_tokens") val promptTokens: Int? = null,
    @SerialName("completion_tokens") val completionTokens: Int? = null,
    @SerialName("cached_tokens") val cachedTokens: Int? = null,
    @SerialName("cost_usd") val costUsd: Double? = null,
    @SerialName("cost_basis") val costBasis: String? = null,
    @SerialName("latency_s") val latencyS: Double? = null,
    val scope: String? = null,
) {
    /** The measured failure this product exists for: budget spent reasoning, nothing returned. */
    val isEmpty: Boolean get() = contentChars == 0
}

@Serializable
data class DecisionResult(
    @SerialName("cost_usd") val costUsd: Double? = null,
    @SerialName("cost_basis") val costBasis: String? = null,
    val calls: Int = 0,
    @SerialName("calls_unknown_cost") val callsUnknownCost: Int = 0,
)

@Serializable
data class Decision(
    val id: String = "",
    val t: Double = 0.0,
    val requested: String = "",
    val client: String? = null,
    val ask: Ask = Ask(),
    val choice: Choice = Choice(),
    @SerialName("earlier_decisions") val earlierDecisions: List<Choice> = emptyList(),
    val attempts: List<Attempt> = emptyList(),
    val providers: List<ProviderState> = emptyList(),
    val dashboard: Map<String, String> = emptyMap(),
    val status: String = "",
    @SerialName("elapsed_s") val elapsedS: Double? = null,
    val result: DecisionResult = DecisionResult(),
)

@Serializable
data class DecisionRow(
    val id: String = "",
    val t: Double = 0.0,
    val requested: String = "",
    val outcome: String = "",
    val seat: String? = null,
    val because: String = "",
    @SerialName("cost_usd") val costUsd: Double? = null,
    @SerialName("cost_basis") val costBasis: String? = null,
    val status: String = "",
) {
    val isRefusal: Boolean get() = status.equals("ABSTAINED", true) || status.equals("FAILED", true)
}

// ---- /router/explain ----------------------------------------------------------------------------

@Serializable
data class ExplainResponse(
    val choice: Choice = Choice(),
    val providers: List<ProviderState> = emptyList(),
    val dashboard: Map<String, String> = emptyMap(),
)

@Serializable
data class ExplainMessage(val role: String = "user", val content: String = "")

@Serializable
data class ExplainRequest(
    val model: String = "auto",
    val messages: List<ExplainMessage>,
    @SerialName("max_tokens") val maxTokens: Int? = null,
    val tools: List<kotlinx.serialization.json.JsonElement>? = null,
)
