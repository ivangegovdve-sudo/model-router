package modelrouter.app

import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertFailsWith
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.double
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long

/**
 * Parses the real fixtures captured from a live router (see docs/fixtures and docs/CONTRACT.md)
 * and checks the contract's hard rendering rules: null price is UNKNOWN (never $0, never sorted
 * cheapest), cost always carries its basis, and an attempt with content_chars == 0 is EMPTY even
 * at HTTP 200.
 */
class FixtureTest {
    private fun fixture(name: String): File = listOf(
        "../../docs/fixtures/$name",
        "../docs/fixtures/$name",
        "docs/fixtures/$name",
    ).map(::File).first { it.exists() }

    private fun text(name: String) = fixture(name).readText(Charsets.UTF_8)

    // ---- decision-route.json: a ROUTE outcome with one successful attempt --------------------

    @Test fun decodesRouteDecision() {
        val d = RouterJson.decodeFromString(Decision.serializer(), text("decision-route.json"))
        assertEquals("20260926T031159-c4ee0e", d.id)
        assertEquals("auto", d.requested)
        assertEquals("ANSWERED", d.status)
        assertEquals(17, d.ask.promptTokens)
        assertEquals(1200, d.ask.maxTokens)
        assertFalse(d.ask.needsTools)
        assertEquals(5.0, d.ask.ceilingUsdPerMtok)

        val choice = d.choice
        assertEquals("ROUTE", choice.outcome)
        assertTrue(choice.isRoute)
        assertEquals("venice:e2ee-qwen-2-5-7b-p", choice.seat)
        assertEquals(8, choice.qualifying.size)
        assertEquals("venice:e2ee-qwen-2-5-7b-p", choice.qualifying.first().seat, "QUALIFIES is ranked, winner first")
        assertTrue(choice.considered.count { it.verdict == "UNKNOWN" } > 0)
        assertEquals(967, choice.facts.rosterSize)
        assertEquals(920, choice.facts.unknownNotListed)

        assertEquals(1, d.attempts.size)
        val a = d.attempts.first()
        assertEquals("venice:e2ee-qwen-2-5-7b-p", a.seat)
        assertEquals(200, a.http)
        assertTrue(a.ok)
        assertEquals(185, a.contentChars)
        assertFalse(a.isEmpty)
        assertEquals("billed", a.costBasis)
        assertEquals(6.63e-06, a.costUsd)

        assertEquals(6.63e-06, d.result.costUsd)
        assertEquals("billed", d.result.costBasis)
        assertEquals(1, d.result.calls)
        assertEquals(5, d.providers.size)
        assertEquals(JsonPrimitive("ok (676 rows)"), d.dashboard["openrouter"])
        assertNull(choice.confidence, "legacy choices do not imply evidence certainty")
        assertNull(choice.facts.requirements)
    }

    // ---- decision-abstain.json: an ABSTAIN outcome, a refusal, never a gap -------------------

    @Test fun decodesAbstainDecisionAsARefusal() {
        val d = RouterJson.decodeFromString(Decision.serializer(), text("decision-abstain.json"))
        assertEquals("ABSTAINED", d.status)
        assertTrue(Fmt.statusIsRefusal(d.status))
        val choice = d.choice
        assertEquals("ABSTAIN", choice.outcome)
        assertFalse(choice.isRoute)
        assertNull(choice.seat)
        assertNull(choice.expectedUsd)
        assertTrue(choice.because.contains("no candidate qualifies"))
        assertEquals(listOf("emits", "supports_tools"), choice.unknown)
        assertEquals(0, choice.qualifying.size)
        assertTrue(choice.excluded.isNotEmpty())
        assertTrue(choice.unknownSeats.isNotEmpty())
        assertEquals(844, choice.facts.unknownNotListed)
        assertEquals(29, choice.facts.freeTierNotListed)
        assertTrue(d.attempts.isEmpty())
        assertNull(d.result.costUsd)
        assertNull(d.result.costBasis)
    }

    // ---- decisions.json: the list feed behind the Decisions screen ---------------------------

    @Test fun decodesDecisionRowList() {
        val rows = RouterJson.decodeFromString(
            kotlinx.serialization.builtins.ListSerializer(DecisionRow.serializer()),
            text("decisions.json"),
        )
        assertEquals(8, rows.size)
        assertEquals("20260926T031201-8228da", rows.first().id)
        assertEquals("ABSTAIN", rows.first().outcome)
        assertNull(rows.first().seat)
        assertTrue(rows.first().isRefusal)
        val routed = rows.first { it.outcome == "ROUTE" }
        assertFalse(routed.isRefusal)
        assertEquals("venice:e2ee-qwen-2-5-7b-p", routed.seat)
        // one row is priced "computed" rather than "billed"
        assertTrue(rows.any { it.costBasis == "computed" })
    }

    // ---- roster-measured.json: the Roster screen's feed --------------------------------------

    @Test fun decodesRosterResponse() {
        val r = RouterJson.decodeFromString(RosterResponse.serializer(), text("roster-measured.json"))
        assertEquals(5, r.providers.size)
        assertEquals(12, r.seats.size)
        val venice = r.seats.first { it.seat == "venice:e2ee-qwen-2-5-7b-p" }
        assertEquals("content", venice.emits)
        assertEquals(0.05164656697218203, venice.measuredUsdPerMtok)
        assertEquals("provider-catalogue", venice.priceSource)
        assertNull(venice.supportsReasoning)
        assertNull(venice.reasoningEfforts)
        assertNull(venice.supportedParameters)
        assertNull(venice.inputModalities)
        assertNull(venice.outputModalities)
        assertNull(venice.supportsEditing)
        // free-tier seats are priced $0, not UNKNOWN: null and zero are different facts
        val free = r.seats.first { it.seat == "openrouter:dots-studio/dots-3-note-preview:free" }
        assertEquals(0.0, free.measuredUsdPerMtok)
        assertFalse(Fmt.perMtok(free.measuredUsdPerMtok).unknown)
    }

    // ---- setup.json: the Setup screen's feed --------------------------------------------------

    @Test fun decodesSetupResponse() {
        val s = RouterJson.decodeFromString(Setup.serializer(), text("setup.json"))
        assertTrue(s.ready)
        assertEquals("gcp", s.secretsSource)
        assertEquals("forest-family-cloud", s.gcpProject)
        assertEquals(5, s.keys.size)
        assertTrue(s.keys.all { it.present })
        assertEquals("disabled (loopback only)", s.auth)
        assertTrue(s.configProblems.isEmpty())
        // no endpoint ever returns a key value; the client only ever sees the secret's name
        assertTrue(s.keys.none { it.name.isBlank() })
    }

    @Test fun dashboardStatusesDecodeMixedEvidenceAndLegacyScalars() {
        val statuses = """{"openrouter":"ok (2 rows)", "nous":{"status":"ok","rows":2}, "sail":null}"""
        val mixed = """{"dashboard":$statuses}"""
        val dashboards = listOf(
            RouterJson.decodeFromString(Setup.serializer(), """{"dashboard_status":$statuses}""").dashboardStatus,
            RouterJson.decodeFromString(RosterResponse.serializer(), mixed).dashboard,
            RouterJson.decodeFromString(ExplainResponse.serializer(), mixed).dashboard,
            RouterJson.decodeFromString(Decision.serializer(), mixed).dashboard,
        )
        dashboards.forEach { dashboard ->
            assertEquals(JsonPrimitive("ok (2 rows)"), dashboard["openrouter"])
            assertEquals(JsonPrimitive(2), dashboard["nous"]?.jsonObject?.get("rows"))
            assertEquals(JsonNull, dashboard["sail"])
        }

        val scalar = """{"dashboard":"not configured","dashboard_status":"not configured"}"""
        val oldDashboards = listOf(
            RouterJson.decodeFromString(Setup.serializer(), """{"dashboard_status":"not configured"}""").dashboardStatus,
            RouterJson.decodeFromString(RosterResponse.serializer(), scalar).dashboard,
            RouterJson.decodeFromString(ExplainResponse.serializer(), scalar).dashboard,
            RouterJson.decodeFromString(Decision.serializer(), scalar).dashboard,
        )
        oldDashboards.forEach { assertEquals(mapOf("status" to JsonPrimitive("not configured")), it) }
        assertTrue(RouterJson.decodeFromString(ExplainResponse.serializer(), """{"dashboard":null}""").dashboard.isEmpty())
    }

    @Test fun nullableCapabilitiesDoNotBecomeUnsupported() {
        val unknown = RouterJson.decodeFromString(Candidate.serializer(), """{
            "available":null,"supports_tools":null,"supports_reasoning":null,"reasoning_efforts":null,
            "supported_parameters":null,"input_modalities":null,"output_modalities":null,"supports_editing":null
        }""")
        assertNull(unknown.available)
        assertNull(unknown.supportsTools)
        assertNull(unknown.supportsReasoning)
        assertNull(unknown.reasoningEfforts)
        assertNull(unknown.supportedParameters)
        assertNull(unknown.inputModalities)
        assertNull(unknown.outputModalities)
        assertNull(unknown.supportsEditing)

        val known = RouterJson.decodeFromString(Candidate.serializer(), """{
            "available":false,"supports_tools":false,"supports_reasoning":true,"reasoning_efforts":[],
            "supported_parameters":["response_format"],"input_modalities":["text","image"],
            "output_modalities":["text"],"supports_editing":false
        }""")
        assertEquals(false, known.available)
        assertEquals(false, known.supportsTools)
        assertEquals(true, known.supportsReasoning)
        assertEquals(emptyList(), known.reasoningEfforts, "an empty level list differs from unknown reasoning support")
        assertEquals(listOf("response_format"), known.supportedParameters)
        assertEquals(listOf("text", "image"), known.inputModalities)
        assertEquals(listOf("text"), known.outputModalities)
        assertEquals(false, known.supportsEditing)
    }

    @Test fun evidenceCertaintyDoesNotInventCalibratedProbability() {
        val choice = RouterJson.decodeFromString(Choice.serializer(), """{
            "outcome":"ROUTE","confidence":{
                "level":"high","basis":"Known catalogue and measured evidence",
                "evidence":["supports_tools=true","measured price"],"unknown_count":0,
                "sample_count":null,"probability":null
            }
        }""")
        val confidence = assertNotNull(choice.confidence)
        assertEquals("high", confidence.level)
        assertEquals("Known catalogue and measured evidence", confidence.basis)
        assertEquals(listOf("supports_tools=true", "measured price"), confidence.evidence)
        assertEquals(0, confidence.unknownCount)
        assertNull(confidence.sampleCount)
        assertNull(confidence.probability, "high evidence certainty is not a success probability")
    }

    @Test fun choicePreservesObservedRequestFeaturesAndRequirements() {
        val choice = RouterJson.decodeFromString(Choice.serializer(), """{
            "facts":{
                "lane":"background",
                "router_requirements":{"providers":["venice"],"model_types":["text"],
                    "required_capabilities":["tools"],"thinking_level":null,"editable":false,
                    "tool_calls":true,"min_context_tokens":16384,"max_usd_per_mtok":0.5},
                "request_features":{"prompt_tokens_estimate":15,
                    "prompt_tokens_basis":"serialized_messages_and_tools_chars_div_4",
                    "message_count":2,"role_counts":{"system":1,"user":1},
                    "tool_count":1,"requested_effort":null,"context_needed_tokens":527,
                    "future_feature":["kept"]}
            }
        }""")
        assertEquals("background", choice.facts.lane)
        val requirements = assertNotNull(choice.facts.requirements)
        assertEquals(listOf("venice"), requirements.providers)
        assertEquals(listOf("text"), requirements.modelTypes)
        assertEquals(listOf("tools"), requirements.requiredCapabilities)
        assertNull(requirements.thinkingLevel)
        assertEquals(false, requirements.editable)
        assertEquals(true, requirements.toolCalls)
        assertEquals(16384L, requirements.minContextTokens)
        assertEquals(0.5, requirements.maxUsdPerMtok)
        val features = choice.facts.requestFeatures
        assertEquals(JsonPrimitive(15), features["prompt_tokens_estimate"])
        assertEquals(JsonPrimitive("serialized_messages_and_tools_chars_div_4"), features["prompt_tokens_basis"])
        assertEquals(JsonPrimitive(1), features["role_counts"]?.jsonObject?.get("system"))
        assertEquals(JsonNull, features["requested_effort"])
        assertEquals(listOf(JsonPrimitive("kept")), features["future_feature"]?.jsonArray?.toList())
    }

    @Test fun explainRequestEncodesExplicitFiltersWithoutDroppingFalseOrZero() {
        val request = ExplainRequest(
            model = "auto:batch",
            messages = listOf(ExplainMessage(content = "Return a JSON object")),
            routerRequirements = RouterRequirements(
                providers = listOf("openrouter", "venice"),
                modelTypes = listOf("text", "reasoning", "vision"),
                requiredCapabilities = listOf("tools", "reasoning", "structured_output", "editing"),
                thinkingLevel = "none",
                editable = false,
                toolCalls = false,
                minContextTokens = 4_294_967_296,
                maxUsdPerMtok = 0.0,
            ),
        )
        val encoded = RouterJson.parseToJsonElement(RouterJson.encodeToString(ExplainRequest.serializer(), request)).jsonObject
        assertEquals("auto:batch", encoded.getValue("model").jsonPrimitive.content)
        val filters = encoded.getValue("router_requirements").jsonObject
        assertEquals(listOf("openrouter", "venice"), filters.getValue("providers").jsonArray.map { it.jsonPrimitive.content })
        assertEquals(listOf("text", "reasoning", "vision"), filters.getValue("model_types").jsonArray.map { it.jsonPrimitive.content })
        assertEquals(listOf("tools", "reasoning", "structured_output", "editing"), filters.getValue("required_capabilities").jsonArray.map { it.jsonPrimitive.content })
        assertEquals("none", filters.getValue("thinking_level").jsonPrimitive.content)
        assertFalse(filters.getValue("editable").jsonPrimitive.boolean)
        assertFalse(filters.getValue("tool_calls").jsonPrimitive.boolean)
        assertEquals(4_294_967_296, filters.getValue("min_context_tokens").jsonPrimitive.long)
        assertEquals(0.0, filters.getValue("max_usd_per_mtok").jsonPrimitive.double)
        assertEquals(request, RouterJson.decodeFromString(ExplainRequest.serializer(), encoded.toString()))
    }

    @Test fun explainFormRejectsInvalidNumericInput() {
        listOf("0", "-1", "1.5", "oops", "2147483648").forEach { value ->
            assertFailsWith<IllegalArgumentException>("invalid answer budget: $value") {
                ExplainForm(prompt = "Hi", maxTokens = value).request()
            }
        }
        listOf("0", "-1", "1.5", "oops", "9223372036854775808").forEach { value ->
            assertFailsWith<IllegalArgumentException>("invalid minimum context: $value") {
                ExplainForm(prompt = "Hi", minContextTokens = value).request()
            }
        }
        listOf("-0.01", "NaN", "Infinity", "1e309", "oops").forEach { value ->
            assertFailsWith<IllegalArgumentException>("invalid price ceiling: $value") {
                ExplainForm(prompt = "Hi", maxUsdPerMtok = value).request()
            }
        }
    }

    @Test fun explainFormRejectsContradictoryCapabilityFilters() {
        val conflicts = listOf(
            "supplied tool with tool calls excluded" to ExplainForm(
                prompt = "Hi", includeTool = true, toolCalls = false,
            ),
            "tool capability both required and excluded" to ExplainForm(
                prompt = "Hi", capabilities = listOf("tools"), toolCalls = false,
            ),
            "editing capability both required and excluded" to ExplainForm(
                prompt = "Hi", capabilities = listOf("editing"), editable = false,
            ),
        )
        conflicts.forEach { (description, form) ->
            assertFailsWith<IllegalArgumentException>(description) { form.request() }
        }
    }

    @Test fun explainFormUsesSeatOrLaneAndBuildsAValidToolDefinition() {
        listOf("interactive", "background", "batch").forEach { lane ->
            assertEquals("auto:$lane", ExplainForm(prompt = "Hi", lane = lane).request().model)
        }
        val request = ExplainForm(
            prompt = "Look up this value", seat = "openrouter:org/model:free", lane = "batch",
            maxTokens = " 1200 ", minContextTokens = " 16384 ", maxUsdPerMtok = " 0 ",
            includeTool = true, thinkingLevel = "low", editable = true, toolCalls = true,
        ).request()
        assertEquals("openrouter:org/model:free", request.model)
        assertEquals(1200, request.maxTokens)
        assertEquals(16384L, request.routerRequirements?.minContextTokens)
        assertEquals(0.0, request.routerRequirements?.maxUsdPerMtok)
        val tool = assertNotNull(request.tools).single().jsonObject
        assertEquals("function", tool.getValue("type").jsonPrimitive.content)
        val function = tool.getValue("function").jsonObject
        assertEquals("lookup", function.getValue("name").jsonPrimitive.content)
        val parameters = function.getValue("parameters").jsonObject
        assertEquals("object", parameters.getValue("type").jsonPrimitive.content)
        assertEquals("string", parameters.getValue("properties").jsonObject.getValue("query").jsonObject.getValue("type").jsonPrimitive.content)
        assertEquals(listOf(JsonPrimitive("query")), parameters.getValue("required").jsonArray.toList())
    }

    @Test fun explainFormDiscardsDisabledFilterInput() {
        val request = ExplainForm(
            prompt = "Hi", filtersEnabled = false, providers = listOf("venice"),
            minContextTokens = "invalid", maxUsdPerMtok = "NaN",
            includeTool = true, capabilities = listOf("tools", "editing"),
            toolCalls = false, editable = false,
        ).request()
        assertNull(request.routerRequirements)
        assertEquals(1, request.tools?.size, "request tools remain present while saved filters are paused")
        val encoded = RouterJson.parseToJsonElement(RouterJson.encodeToString(ExplainRequest.serializer(), request)).jsonObject
        assertFalse("router_requirements" in encoded)
    }

    // ---- hard rendering rules -----------------------------------------------------------------

    @Test fun nullPriceIsUnknownNeverZero() {
        val unknown = Fmt.perMtok(null)
        assertEquals("UNKNOWN", unknown.text)
        assertTrue(unknown.unknown)
        val zero = Fmt.perMtok(0.0)
        assertEquals(false, zero.unknown, "a real $0 (free tier) is distinct from UNKNOWN")
    }

    @Test fun costAlwaysCarriesItsBasis() {
        val billed = Fmt.cost(6.63e-06, "billed")
        assertEquals("billed", billed.basis)
        assertFalse(billed.unknown)
        val unknown = Fmt.cost(null, null)
        assertEquals("UNKNOWN", unknown.amount)
        assertTrue(unknown.unknown)
    }

    @Test fun attemptWithZeroContentCharsIsEmptyEvenAtHttp200() {
        val reasonedButEmpty = Attempt(seat = "x", http = 200, ok = true, contentChars = 0, reasoningChars = 400)
        assertTrue(reasonedButEmpty.isEmpty)
        assertEquals("EMPTY", Fmt.attemptLabel(reasonedButEmpty))
        val real = Attempt(seat = "x", http = 200, ok = true, contentChars = 185, reasoningChars = 0)
        assertFalse(real.isEmpty)
        assertEquals("OK", Fmt.attemptLabel(real))
        val failed = Attempt(seat = "x", http = 500, ok = false, contentChars = 12)
        assertEquals("FAILED", Fmt.attemptLabel(failed))
    }

    @Test fun abstainAndFailedAreRefusalsNeverAGap() {
        assertTrue(Fmt.statusIsRefusal("ABSTAINED"))
        assertTrue(Fmt.statusIsRefusal("FAILED"))
        assertFalse(Fmt.statusIsRefusal("ANSWERED"))
        assertFalse(Fmt.statusIsRefusal("STREAMING"))
    }

    @Test fun tinyCostsNeverReadAsFree() {
        assertEquals("$0.00000025", Fmt.money(2.5e-7))
        assertEquals("$0.0000081", Fmt.money(8.1e-6))
        assertEquals("$0.000459", Fmt.money(4.59e-4))
        assertEquals("$0", Fmt.money(0.0))
    }
}
