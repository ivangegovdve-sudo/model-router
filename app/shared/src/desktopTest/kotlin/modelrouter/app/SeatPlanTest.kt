package modelrouter.app

import kotlinx.serialization.json.*
import kotlin.test.*

class SeatPlanTest {
    private val glass = SeatPlanTemplate("glass-pr-solver", name = "Glass PR solver", consumer = "glass-solver", context = buildJsonObject { put("kind", "pull-request") }, slots = listOf(
        PlanSlot("review_a", role = "review", requirements = RouterRequirements(toolCalls = true), lane = "background"),
        PlanSlot("review_b", role = "review", requirements = RouterRequirements(toolCalls = true), lane = "background"),
        PlanSlot("fixer", role = "fix", requirements = RouterRequirements(toolCalls = true), lane = "background"),
        PlanSlot("rebase", role = "rebase", requirements = RouterRequirements(toolCalls = true), lane = "background"),
    ), references = listOf(PlanReference("pr-author")), constraints = listOf(
        PlanConstraint("review-pair", slots = listOf("review_a", "review_b")),
        PlanConstraint("review-a-vs-author", kind = "different_from", slot = "review_a", others = listOf(PlanPeer(reference = "pr-author"))),
        PlanConstraint("review-b-vs-author", kind = "different_from", slot = "review_b", others = listOf(PlanPeer(reference = "pr-author"))),
    ))
    private val council = SeatPlanTemplate("council-roundtable", name = "Council roundtable", consumer = "private-council", slots = (1..4).map {
        PlanSlot("member_$it", roles = listOf("proposer", "critic") + (if (it == 1) listOf("chair") else emptyList()) + (if (it == 4) listOf("triage") else emptyList()), requirements = RouterRequirements(), lane = "background")
    }, constraints = listOf(PlanConstraint("council-diversity", slots = (1..4).map { "member_$it" })))

    @Test fun goalSelectionPreservesUserTextAndExplicitFilters() {
        val current = ExplainForm(prompt = "My own private request", maxTokens = "777", maxUsdPerMtok = "0", thinkingLevel = "low", providers = listOf("sail"))
        val next = applyGoalDefaults(current, GoalTemplates.first(), setOf("maxTokens", "maxUsdPerMtok", "thinkingLevel", "providers"))
        assertEquals(current.prompt, next.prompt)
        assertEquals("777", next.maxTokens)
        assertEquals("0", next.maxUsdPerMtok)
        assertEquals("low", next.thinkingLevel)
        assertEquals(listOf("sail"), next.providers)
        assertEquals("background", next.lane)
    }

    @Test fun uneditedGoalTextChangesButEditedDefaultTextStays() {
        val review = GoalTemplates.first()
        val quick = GoalTemplates.first { it.id == "quick-answer" }
        assertEquals(quick.defaults.prompt, applyGoalDefaults(review.defaults, quick).prompt)
        assertEquals(review.defaults.prompt, applyGoalDefaults(review.defaults, quick, setOf("prompt")).prompt)
        assertNull(quick.defaults.thinkingLevel, "Quick answers should not require controllable reasoning-none support")
    }

    @Test fun constraintEncodingMatchesTheStrictBackendKindKeys() {
        val request = SeatPlanDraft().applyTemplate(glass).request()
        val rules = RouterJson.encodeToJsonElement(SeatPlanRequest.serializer(), request).jsonObject.getValue("constraints").jsonArray
        rules.forEach { element ->
            val obj = element.jsonObject
            if (obj.getValue("kind").jsonPrimitive.content == "all_distinct") assertEquals(setOf("id", "kind", "dimension", "slots"), obj.keys)
            else assertEquals(setOf("id", "kind", "dimension", "slot", "others"), obj.keys)
        }
    }

    @Test fun templatedSlotsUseSharedDefaultsAndExpandCustomCount() {
        val draft = SeatPlanDraft().applyTemplate(council)
        assertTrue(draft.slots.all { it.useDefaults })
        val changed = draft.copy(defaults = draft.defaults.copy(toolCalls = false, maxUsdPerMtok = "0"), defaultsEdited = true).resize(7)
        val request = changed.request()
        assertEquals(7, request.slots.size)
        assertTrue(request.slots.all { it.requirements?.toolCalls == false && it.requirements.maxUsdPerMtok == 0.0 })
        assertEquals(request.slots.map { it.id }, request.constraints.single().slots)
        assertTrue(changed.slots.last().roles.contains("triage"))
        assertFalse(changed.slots[3].roles.contains("triage"))
    }

    @Test fun pairsTriplesAndAllGroupsPreserveExactOverlaps() {
        val draft = SeatPlanDraft().applyTemplate(glass).copy(groups = listOf(GroupDraft("pair", listOf("review_a", "review_b")), GroupDraft("triple", listOf("review_b", "fixer", "rebase"))), allDistinct = true)
        val rules = draft.request().constraints
        assertEquals(listOf("review_a", "review_b"), rules.first { it.id == "pair" }.slots)
        assertEquals(listOf("review_b", "fixer", "rebase"), rules.first { it.id == "triple" }.slots)
        assertEquals(4, rules.first { it.id == "all-seats" }.slots.size)
    }

    @Test fun sameReferenceIdentitiesOnlyCreateRequestedSeatEdges() {
        val draft = SeatPlanDraft().applyTemplate(glass).copy(headSha = "head-1", references = listOf(ReferenceDraft("author", "sail:a", evidenceId = "registered-origin"), ReferenceDraft("fixer-origin", "sail:a", evidenceId = "registered-fixer")), referenceRules = listOf(ReferenceRuleDraft("author-rule", listOf("review_a", "review_b"), "author"), ReferenceRuleDraft("fixer-rule", listOf("review_a"), "fixer-origin")))
        val request = draft.request()
        assertEquals(3, request.constraints.count { it.kind == "different_from" })
        assertTrue(request.constraints.filter { it.kind == "different_from" }.all { it.slot in setOf("review_a", "review_b") && it.others.all { peer -> peer.reference != null && peer.slot == null } })
        assertEquals("head-1", request.references.first().revision)
    }

    @Test fun templateChangesPreserveCustomBindingsCountFiltersAndGroups() {
        val draft = SeatPlanDraft().applyTemplate(glass).resize(5).editSlot("fixer") { it.copy(kind = "app-fixed", seat = "external:fixed", executorId = "registered", useDefaults = false, filters = it.filters.copy(maxUsdPerMtok = "0", editable = false)) }.copy(
            groups = listOf(GroupDraft("council-diversity", listOf("review_b", "fixer", "rebase"))), repo = "owner/repo", headSha = "exact-head",
            referenceRules = listOf(ReferenceRuleDraft("review-a-vs-author", listOf("fixer"), "pr-author")),
        )
        val changed = draft.applyTemplate(council)
        assertEquals(5, changed.slots.size)
        val fixer = changed.slots.first { it.id == "fixer" }
        assertEquals("app-fixed", fixer.kind)
        assertEquals("registered", fixer.executorId)
        assertEquals("0", fixer.filters.maxUsdPerMtok)
        assertEquals(listOf("review_b", "fixer", "rebase"), changed.groups.first { it.id == "council-diversity" }.slots)
        assertEquals(listOf("fixer"), changed.referenceRules.first { it.id == "review-a-vs-author" }.slots)
        assertEquals("exact-head", changed.headSha)
        assertEquals("owner/repo", changed.repo)
    }

    @Test fun removedSeatsKeepAuthoredRulesVisibleUntilRepaired() {
        val draft = SeatPlanDraft().resize(3).copy(groups = listOf(GroupDraft("custom-pair", listOf("seat_1", "seat_3"))))
        val shrunk = draft.resize(2)
        assertEquals(listOf("seat_1", "seat_3"), shrunk.groups.single().slots)
        assertFailsWith<IllegalArgumentException> { shrunk.request() }
    }

    @Test fun slotToSlotTemplateRulesAndDistinctEstimatesSurviveImport() {
        val template = council.copy(slots = council.slots.map { it.copy(estimate = PlanEstimate(64, 128, 2048)) }, constraints = listOf(PlanConstraint("slot-edge", kind = "different_from", slot = "member_1", others = listOf(PlanPeer(slot = "member_2")))))
        val request = SeatPlanDraft().applyTemplate(template).request()
        assertEquals(PlanEstimate(64, 128, 2048), request.slots.first().estimate)
        assertEquals("member_2", request.constraints.single().others.single().slot)
    }

    @Test fun fixedBindingsAreExplicitAndNoPromptIsSerialized() {
        val draft = SeatPlanDraft(defaults = ExplainForm(prompt = "PRIVATE_TASK_BODY", maxTokens = "512"), slots = listOf(
            SlotDraft("a", kind = "router-fixed", seat = "sail:a"),
            SlotDraft("b", kind = "app-fixed", seat = "external:b", executorId = "registered"),
        ))
        val encoded = RouterJson.encodeToJsonElement(SeatPlanRequest.serializer(), draft.request()).jsonObject
        assertFalse(encoded.toString().contains("PRIVATE_TASK_BODY"))
        val slots = encoded.getValue("slots").jsonArray
        assertEquals(setOf("kind", "seat"), slots[0].jsonObject.getValue("binding").jsonObject.keys)
        assertEquals(setOf("kind", "executor_id", "model_ref"), slots[1].jsonObject.getValue("binding").jsonObject.keys)
        assertFalse(slots[0].jsonObject.containsKey("messages"))
    }

    @Test fun countAndBindingValidationNeverSilentlyShrinksAPlan() {
        assertFailsWith<IllegalArgumentException> { SeatPlanDraft().resize(0) }
        assertFailsWith<IllegalArgumentException> { SeatPlanDraft().resize(33) }
        assertEquals(32, SeatPlanDraft().resize(32).request().slots.size)
        assertFailsWith<IllegalArgumentException> { SeatPlanDraft(slots = listOf(SlotDraft("a", kind = "app-fixed", seat = "external:a"))).request() }
        assertFailsWith<IllegalArgumentException> { SeatPlanDraft(slots = listOf(SlotDraft("a"), SlotDraft("a"))).request() }
    }

    @Test fun responsePreservesExactUnknownCostsProvenanceAndConstraintEvidence() {
        val response = RouterJson.decodeFromString(SeatPlanResponse.serializer(), """{
            "outcome":"RESOLVED","plan_id":"p1","version":1,"snapshot_id":"snap",
            "assignments":[{"slot_id":"a","role":"review","roles":["review"],"binding":{"kind":"auto"},"seat":"sail:a","weights_id":"W1","estimated_usd":"0.000000000000123456789","invoke":{"api":"chat.completions"}},
                           {"slot_id":"b","role":"fix","binding":{"kind":"app-fixed","executor_id":"registered","model_ref":"external:b"},"weights_id":"W2","estimated_usd":null}],
            "constraint_evidence":[{"id":"pair","kind":"all_distinct","satisfied":true,"pairs":[{"left":{"slot":"a"},"right":{"slot":"b"},"left_weights_id":"W1","right_weights_ids":["W2"],"satisfied":true}]}],
            "references":{"pr-author":{"provenance":"operator-configured","revision":"head","unknown":[]}},
            "estimates":{"known_subtotal_usd":"0.000000000000123456789","total_usd":null}
        }""")
        assertEquals("0.000000000000123456789", response.assignments.first().estimatedUsd)
        assertNull(response.assignments[1].estimatedUsd)
        assertEquals(JsonNull, response.estimates["total_usd"])
        assertEquals("operator-configured", response.references.getValue("pr-author").jsonObject.getValue("provenance").jsonPrimitive.content)
        assertEquals(true, response.constraintEvidence.single().pairs.single().satisfied)
        assertFalse(RouterJson.encodeToString(SeatPlanResponse.serializer(), response).contains("probability"))
    }

    @Test fun genericReferenceDoesNotInventPullRequestContext() {
        val request = SeatPlanDraft(references = listOf(ReferenceDraft("reference", "sail:a")), referenceRules = listOf(ReferenceRuleDraft("ref", listOf("seat_1"), "reference"))).request()
        assertFalse(request.context.containsKey("kind"))
        assertFalse(request.context.containsKey("head_sha"))
    }

    @Test fun importedRequestRetainsSeparateEstimatesAndExplicitBindings() {
        val original = SeatPlanDraft().applyTemplate(glass).request().copy(slots = listOf(PlanSlot("a", role = "review", roles = listOf("review"), binding = PlanBinding("router-fixed", seat = "sail:a"), requirements = RouterRequirements(toolCalls = false, maxUsdPerMtok = 0.0), estimate = PlanEstimate(50, 128, 2048), lane = "batch")), constraints = emptyList(), references = emptyList(), template = null)
        val roundtrip = SeatPlanDraft.fromRequest(original).request()
        assertEquals(original.slots.single(), roundtrip.slots.single())
        assertEquals(original.context, roundtrip.context)
    }
}
