package modelrouter.app

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.contentOrNull

data class SlotDraft(
    val id: String,
    val roles: String = "member",
    val kind: String = "auto",
    val seat: String = "",
    val executorId: String = "",
    val useDefaults: Boolean = true,
    val filters: ExplainForm = ExplainForm(maxTokens = "1024", lane = "background"),
    val edited: Boolean = false,
    val estimate: PlanEstimate? = null,
)

data class GroupDraft(val id: String, val slots: List<String> = emptyList(), val custom: Boolean = true)
data class ReferenceDraft(val id: String, val models: String = "", val revision: String = "", val evidenceId: String = "")
data class ReferenceRuleDraft(val id: String, val slots: List<String> = emptyList(), val reference: String = "pr-author", val custom: Boolean = true)

/** Explicit editable slots and edges. This class assembles requests; it never assigns models. */
data class SeatPlanDraft(
    val consumer: String = "private-council",
    val policy: String = "cheapest",
    val template: PlanTemplateRef? = null,
    val defaults: ExplainForm = ExplainForm(maxTokens = "1024", lane = "background", toolCalls = true, maxUsdPerMtok = "2.00"),
    val defaultsEdited: Boolean = false,
    val countEdited: Boolean = false,
    val promptTokens: String = "700",
    val context: JsonObject = JsonObject(emptyMap()),
    val repo: String = "",
    val number: String = "",
    val headSha: String = "",
    val slots: List<SlotDraft> = listOf(SlotDraft("seat_1")),
    val groups: List<GroupDraft> = emptyList(),
    val references: List<ReferenceDraft> = emptyList(),
    val referenceRules: List<ReferenceRuleDraft> = emptyList(),
    val retainedConstraints: List<PlanConstraint> = emptyList(),
    val allDistinct: Boolean = false,
) {
    companion object {
        fun fromRequest(request: SeatPlanRequest): SeatPlanDraft = SeatPlanDraft(
            consumer = request.consumer, policy = request.policy ?: "cheapest", template = request.template, context = request.context,
            repo = request.context["repo"]?.jsonPrimitive?.contentOrNull.orEmpty(), number = request.context["number"]?.jsonPrimitive?.contentOrNull.orEmpty(), headSha = request.context["head_sha"]?.jsonPrimitive?.contentOrNull.orEmpty(),
            slots = request.slots.map { slot -> SlotDraft(slot.id, roles = slot.roles.ifEmpty { listOfNotNull(slot.role) }.joinToString(", "), kind = slot.binding.kind, seat = slot.binding.seat ?: slot.binding.modelRef.orEmpty(), executorId = slot.binding.executorId.orEmpty(), useDefaults = false, filters = (slot.requirements?.toForm() ?: ExplainForm()).copy(lane = slot.lane ?: "background", maxTokens = slot.estimate?.maxTokens?.toString().orEmpty()), edited = true, estimate = slot.estimate) },
            groups = request.constraints.filter { it.kind == "all_distinct" }.map { GroupDraft(it.id, it.slots) },
            references = request.references.map { ReferenceDraft(it.id, it.models.joinToString(", "), it.revision, it.evidenceId.orEmpty()) },
            referenceRules = request.constraints.filter { it.kind == "different_from" && it.others.none { peer -> peer.slot != null } }.flatMap { rule -> rule.others.mapNotNull { peer -> peer.reference?.let { ReferenceRuleDraft(rule.id, listOfNotNull(rule.slot), it) } } },
            retainedConstraints = request.constraints.filter { it.kind == "different_from" && it.others.any { peer -> peer.slot != null } }, countEdited = true,
        )
    }
    fun editSlot(id: String, change: (SlotDraft) -> SlotDraft) = copy(slots = slots.map { if (it.id == id) change(it).copy(edited = true) else it })

    fun resize(count: Int): SeatPlanDraft {
        require(count in 1..32) { "A plan needs between 1 and 32 seats." }
        var next = slots.take(count)
        var serial = 1
        while (next.size < count) {
            while (next.any { it.id == "seat_$serial" }) serial++
            next = next + SlotDraft("seat_$serial", roles = if (template?.id == "council-roundtable") "proposer, critic" else "review")
        }
        if (template?.id == "council-roundtable") next = next.mapIndexed { index, slot ->
            if (slot.edited) slot else slot.copy(roles = (listOf("proposer", "critic") + (if (index == 0) listOf("chair") else emptyList()) + (if (index == count - 1) listOf("triage") else emptyList())).joinToString(", "))
        }
        return copy(slots = next, countEdited = true, groups = groups.map { if (!it.custom && template?.id == "council-roundtable" && it.id == "council-diversity") it.copy(slots = next.map { slot -> slot.id }) else it }, referenceRules = referenceRules)
    }

    fun applyTemplate(value: SeatPlanTemplate): SeatPlanDraft {
        val first = value.slots.firstOrNull()
        val nextDefaults = if (defaultsEdited) defaults else (first?.requirements?.toForm() ?: defaults).copy(
            lane = first?.lane ?: defaults.lane,
            maxTokens = first?.estimate?.maxTokens?.toString() ?: defaults.maxTokens,
            maxUsdPerMtok = first?.requirements?.maxUsdPerMtok?.toString() ?: defaults.maxUsdPerMtok,
        )
        val preserveSlots = countEdited || slots.any { it.edited } || groups.any { it.custom } || referenceRules.any { it.custom }
        val mapping = value.slots.mapIndexed { index, slot -> slot.id to (if (preserveSlots) slots.getOrNull(index)?.id ?: slot.id else slot.id) }.toMap()
        fun imported(slot: PlanSlot, id: String = slot.id) = SlotDraft(
            id = id, roles = slot.roles.ifEmpty { listOfNotNull(slot.role) }.joinToString(", ").ifBlank { "member" },
            kind = slot.binding.kind, seat = slot.binding.seat ?: slot.binding.modelRef.orEmpty(), executorId = slot.binding.executorId.orEmpty(),
            useDefaults = (slot.requirements == null || slot.requirements == first?.requirements) && (slot.lane == null || slot.lane == first?.lane),
            filters = (slot.requirements?.toForm() ?: nextDefaults).copy(lane = slot.lane ?: nextDefaults.lane, maxTokens = slot.estimate?.maxTokens?.toString() ?: nextDefaults.maxTokens),
            estimate = slot.estimate,
        )
        val nextSlots = if (preserveSlots) slots.mapIndexed { index, old ->
            if (old.edited) old else value.slots.getOrNull(index)?.let { imported(it, old.id) } ?: old
        } else value.slots.map { imported(it) }
        fun mapped(id: String) = mapping[id] ?: id
        val customGroups = groups.filter { it.custom }
        val templateGroups = value.constraints.filter { it.kind == "all_distinct" }.map { rule ->
            GroupDraft(rule.id, if (value.id == "council-roundtable" && rule.id == "council-diversity") nextSlots.map { it.id } else rule.slots.map(::mapped), custom = false)
        }
        val customRules = referenceRules.filter { it.custom }
        val templateRules = value.constraints.filter { it.kind == "different_from" && it.others.none { peer -> peer.slot != null } }.flatMap { rule ->
            rule.others.mapNotNull { peer -> peer.reference?.let { ReferenceRuleDraft(rule.id, listOfNotNull(rule.slot?.let(::mapped)), it, custom = false) } }
        }
        return copy(
            consumer = value.consumer, template = PlanTemplateRef(value.id, value.version), defaults = nextDefaults,
            context = JsonObject(value.context + context), slots = nextSlots,
            groups = templateGroups.filter { incoming -> customGroups.none { it.id == incoming.id } } + customGroups,
            references = value.references.map { ref -> references.firstOrNull { it.id == ref.id } ?: ReferenceDraft(ref.id, ref.models.joinToString(", "), ref.revision, ref.evidenceId.orEmpty()) } + references.filter { old -> value.references.none { it.id == old.id } },
            referenceRules = templateRules.filter { incoming -> customRules.none { it.id == incoming.id } } + customRules,
            retainedConstraints = value.constraints.filter { it.kind == "different_from" && it.others.any { peer -> peer.slot != null } }.map { rule -> rule.copy(slot = rule.slot?.let(::mapped), others = rule.others.map { it.copy(slot = it.slot?.let(::mapped)) }) } + retainedConstraints.filter { old -> value.constraints.none { it.id == old.id } },
        )
    }

    fun request(): SeatPlanRequest {
        require(consumer.isNotBlank()) { "Choose a configured consumer." }
        require(slots.size in 1..32) { "A plan needs between 1 and 32 seats." }
        require(slots.map { it.id }.distinct().size == slots.size && slots.none { it.id.isBlank() }) { "Seat IDs must be unique and nonempty." }
        val promptEstimate = promptTokens.toIntOrNull()?.takeIf { it > 0 } ?: throw IllegalArgumentException("Prompt estimate must be a positive whole number.")
        val ids = slots.map { it.id }.toSet()
        val wireSlots = slots.map { slot ->
            val roles = slot.roles.split(',').map { it.trim() }.filter { it.isNotEmpty() }.distinct()
            require(roles.isNotEmpty()) { "${slot.id}: provide at least one role." }
            val form = if (slot.useDefaults) defaults else slot.filters
            val parsed = form.copy(prompt = "Seat estimate").request()
            val binding = when (slot.kind) {
                "auto" -> PlanBinding()
                "router-fixed" -> { require(slot.seat.contains(':')) { "${slot.id}: choose a provider:model seat." }; PlanBinding(kind = slot.kind, seat = slot.seat.trim()) }
                "app-fixed" -> { require(slot.executorId.isNotBlank() && slot.seat.contains(':')) { "${slot.id}: provide a registered executor and provider:model identity." }; PlanBinding(kind = slot.kind, executorId = slot.executorId.trim(), modelRef = slot.seat.trim()) }
                else -> throw IllegalArgumentException("${slot.id}: unknown binding.")
            }
            PlanSlot(slot.id, role = roles.first(), roles = roles, binding = binding, requirements = parsed.routerRequirements ?: RouterRequirements(), lane = form.lane, estimate = slot.estimate ?: PlanEstimate(promptEstimate, parsed.maxTokens ?: 1024, parsed.maxTokens ?: 1024))
        }
        fun roleCount(role: String) = wireSlots.count { role in it.roles }
        if (template?.id == "glass-pr-solver") require(roleCount("review") >= 2) { "Glass needs at least two review seats. Add a reviewer or choose a custom plan." }
        if (template?.id == "council-roundtable") {
            require(roleCount("proposer") >= 2 && roleCount("critic") >= 1 && roleCount("chair") == 1 && roleCount("triage") == 1) { "Council needs two proposers, a critic, exactly one chair and one triage seat. Roles may share a seat." }
        }
        val rules = groups.map { group ->
            require(group.slots.distinct().size >= 2) { "${group.id}: select at least two seats." }
            require(group.slots.all { it in ids }) { "${group.id}: a selected seat is no longer in this plan." }
            PlanConstraint(group.id, slots = group.slots.distinct())
        }.toMutableList()
        retainedConstraints.forEach { rule ->
            require(rule.slot in ids && rule.others.all { peer -> peer.slot == null || peer.slot in ids }) { "${rule.id}: a required seat is no longer in this plan." }
            rules += rule
        }
        if (allDistinct && slots.size > 1) rules += PlanConstraint("all-seats", slots = slots.map { it.id })
        require(references.map { it.id }.distinct().size == references.size && references.none { it.id.isBlank() }) { "Reference IDs must be unique and nonempty." }
        val referenceIds = references.map { it.id }.toSet()
        referenceRules.forEach { rule ->
            require(rule.slots.isNotEmpty() && rule.slots.all { it in ids }) { "${rule.id}: select seats in this plan." }
            require(rule.reference in referenceIds) { "${rule.id}: choose a reference defined below." }
            rule.slots.forEach { slot -> rules += PlanConstraint("${rule.id}-$slot", kind = "different_from", slot = slot, others = listOf(PlanPeer(reference = rule.reference))) }
        }
        require(rules.map { it.id }.distinct().size == rules.size) { "Constraint IDs must be unique." }
        val fields = context.toMutableMap()
        if (repo.isNotBlank() || number.isNotBlank() || headSha.isNotBlank()) {
            fields["kind"] = JsonPrimitive("pull-request")
            if (repo.isNotBlank()) fields["repo"] = JsonPrimitive(repo.trim())
            if (number.isNotBlank()) fields["number"] = JsonPrimitive(number.toIntOrNull()?.takeIf { it > 0 } ?: throw IllegalArgumentException("PR number must be a positive whole number."))
            if (headSha.isNotBlank()) fields["head_sha"] = JsonPrimitive(headSha.trim())
        }
        return SeatPlanRequest(consumer = consumer.trim(), policy = policy, template = template, context = JsonObject(fields), slots = wireSlots, constraints = rules, references = references.map { ref -> PlanReference(ref.id.trim(), ref.models.split(',').map { it.trim() }.filter { it.isNotEmpty() }, ref.revision.ifBlank { headSha }.trim(), ref.evidenceId.trim().takeIf { it.isNotEmpty() }) })
    }
}

fun RouterRequirements.toForm() = ExplainForm(
    providers = providers, modelTypes = modelTypes, capabilities = requiredCapabilities,
    thinkingLevel = thinkingLevel, editable = editable, toolCalls = toolCalls,
    minContextTokens = minContextTokens?.toString().orEmpty(), maxUsdPerMtok = maxUsdPerMtok?.toString().orEmpty(),
)
