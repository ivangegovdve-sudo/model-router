package modelrouter.app

import io.ktor.client.HttpClient
import io.ktor.client.engine.HttpClientEngineFactory
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.request.HttpRequestBuilder
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.contentType
import io.ktor.http.isSuccess

/** Router connection settings. The token is never logged, printed, or echoed back. */
data class RouterSettings(
    val baseUrl: String = DEFAULT_URL,
    val token: String = "",
) {
    override fun toString(): String = "RouterSettings(baseUrl=$baseUrl, token=<redacted>)"

    companion object { const val DEFAULT_URL = "http://127.0.0.1:7480" }
}

/** Platform persistence for [RouterSettings]; implementations encrypt the token at rest. */
interface SettingsStore {
    fun load(): RouterSettings
    fun save(settings: RouterSettings)
    /** Human-readable description of where and how the token is stored. */
    val storageNote: String
}

class ApiException(val status: Int, message: String) : Exception(message)

/** HTTP engine per platform (CIO everywhere: see shared/build.gradle.kts). */
expect fun httpEngine(): HttpClientEngineFactory<*>

/**
 * Thin client of the modelrouter HTTP contract (docs/CONTRACT.md). It never holds a provider
 * key and never re-implements a routing decision: every screen renders exactly what one of
 * these calls returned.
 */
class RouterApi(private val settings: RouterSettings) {
    // No Logging plugin is installed on purpose: nothing here may print the Authorization header.
    private val client = HttpClient(httpEngine()) {
        expectSuccess = false
        install(HttpTimeout) {
            connectTimeoutMillis = 8_000
            requestTimeoutMillis = 30_000
        }
    }

    private val base = settings.baseUrl.trimEnd('/')

    private fun HttpRequestBuilder.auth() {
        if (settings.token.isNotBlank()) header(HttpHeaders.Authorization, "Bearer ${settings.token}")
    }

    private suspend fun HttpResponse.textOrThrow(): String {
        val body = bodyAsText()
        if (!status.isSuccess()) {
            val hint = when (status.value) {
                401 -> "missing or wrong router token"
                404 -> "not found: is the base URL correct?"
                else -> runCatching {
                    val obj = RouterJson.parseToJsonElement(body).let { it as? kotlinx.serialization.json.JsonObject }
                    val err = obj?.get("error") as? kotlinx.serialization.json.JsonObject
                    (err?.get("message") as? kotlinx.serialization.json.JsonPrimitive)?.content
                }.getOrNull() ?: body.take(300)
            }
            throw ApiException(status.value, "HTTP ${status.value}: $hint")
        }
        return body
    }

    suspend fun health(): Health = RouterJson.decodeFromString(Health.serializer(), client.get("$base/health").textOrThrow())

    suspend fun setup(): Setup = RouterJson.decodeFromString(Setup.serializer(), client.get("$base/router/setup") { auth() }.textOrThrow())

    suspend fun roster(provider: String? = null, measuredOnly: Boolean = false): RosterResponse {
        val qs = buildList {
            provider?.let { add("provider=$it") }
            if (measuredOnly) add("measured_only=true")
        }.joinToString("&").let { if (it.isEmpty()) "" else "?$it" }
        return RouterJson.decodeFromString(RosterResponse.serializer(), client.get("$base/router/roster$qs") { auth() }.textOrThrow())
    }

    suspend fun decisions(limit: Int = 50): List<DecisionRow> =
        RouterJson.decodeFromString(
            kotlinx.serialization.builtins.ListSerializer(DecisionRow.serializer()),
            client.get("$base/router/decisions?limit=$limit") { auth() }.textOrThrow(),
        )

    suspend fun decision(id: String): Decision =
        RouterJson.decodeFromString(Decision.serializer(), client.get("$base/router/decisions/$id") { auth() }.textOrThrow())

    /** Costs nothing: the router judges the request without making a model call. */
    suspend fun explain(req: ExplainRequest): ExplainResponse {
        val resp = client.post("$base/router/explain") {
            auth()
            contentType(ContentType.Application.Json)
            setBody(RouterJson.encodeToString(ExplainRequest.serializer(), req))
        }
        return RouterJson.decodeFromString(ExplainResponse.serializer(), resp.textOrThrow())
    }

    fun close() = client.close()
}
