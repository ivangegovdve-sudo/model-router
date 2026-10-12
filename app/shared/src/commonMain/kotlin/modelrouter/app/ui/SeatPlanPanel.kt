package modelrouter.app.ui

import androidx.compose.foundation.layout.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.graphics.Color
import androidx.compose.foundation.background
import modelrouter.app.*
import kotlinx.serialization.json.*

@Composable
fun SeatPlanScreen(ctl: AppController) {
    LaunchedEffect(ctl.settings) {
        if (ctl.planTemplates == null && ctl.planTemplatesError == null) ctl.loadPlanTemplates()
        if (ctl.explainRoster == null && ctl.explainRosterError == null) ctl.loadExplainRoster()
    }
    LaunchedEffect(ctl.planTemplates) {
        if (ctl.planDraft.template == null) ctl.planTemplates?.templates?.firstOrNull { it.id == "glass-pr-solver" }?.let(ctl::choosePlanTemplate)
    }
    Column(Modifier.fillMaxSize()) {
        RouteTabs(ctl)
        ComposerLayout(sidebar = { PlanGoals(ctl) }, editor = { PlanEditor(ctl) }, evidence = { PlanEvidence(ctl) })
    }
}

@Composable
private fun PlanGoals(ctl: AppController) {
    T("Goal templates", Ink.type.heading)
    Gap(18.dp)
    ctl.planTemplates?.templates?.forEach { template ->
        val description = when (template.id) {
            "glass-pr-solver" -> "Two reviewers, plus fix and rebase seats."
            "council-roundtable" -> "Set members, chair and weight diversity."
            else -> "Configure roles and exact model-weight rules."
        }
        GoalCard(template.name.ifBlank { template.id }, description, ctl.planDraft.template?.id == template.id) { ctl.choosePlanTemplate(template) }
    }
    GoalCard("Custom seat plan", "Choose your own roles and groups.", ctl.planDraft.template == null) {
        ctl.planDraft = ctl.planDraft.copy(template = null)
    }
    GoalCard("Single-model goals", "Code review, quick answer, research and more.", false) { ctl.screen = Screen.Explain }
    if (ctl.planTemplatesLoading) T("Loading router templates…", Ink.type.small, Ink.colors.fgMuted)
    ctl.planTemplatesError?.let { T("Templates unavailable: $it", Ink.type.small, Ink.colors.amberText); Gap(8.dp); RouterButton("Retry templates", { ctl.loadPlanTemplates() }) }
    Gap(18.dp)
    T("Templates supply explicit slots and rules. Your custom edits are preserved.", Ink.type.small, Ink.colors.fgMuted)
}

@Composable
private fun PlanEditor(ctl: AppController) {
    val c = Ink.colors
    val draft = ctl.planDraft
    var editingDefaults by remember { mutableStateOf(false) }
    var editingSlot by remember { mutableStateOf<String?>(null) }
    var showContext by remember { mutableStateOf(draft.references.isNotEmpty()) }
    val error = runCatching { draft.request() }.exceptionOrNull()?.message
    T("Configure your seats", Ink.type.title)
    Gap(5.dp)
    T("Choose roles, then specify which model weights must differ.", Ink.type.body, c.fgMuted)
    Gap(16.dp)
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        StatusChip(ctl.planTemplates?.templates?.firstOrNull { it.id == draft.template?.id }?.name ?: "Custom seat plan", ChipKind.Selected)
        RouterButton("−", { ctl.planDraft = draft.resize(draft.slots.size - 1) }, enabled = draft.slots.size > 1)
        StatusChip("${draft.slots.size} seats", ChipKind.Neutral)
        RouterButton("+", { ctl.planDraft = draft.resize(draft.slots.size + 1) }, enabled = draft.slots.size < 32)
        RouterButton("Add seat", { ctl.planDraft = draft.resize(draft.slots.size + 1) }, enabled = draft.slots.size < 32)
    }
    Gap(16.dp)
    UiCard {
        T("Seats", Ink.type.heading)
        Gap(10.dp)
        draft.slots.forEachIndexed { index, slot ->
            if (index > 0) { Gap(10.dp); HRule(); Gap(10.dp) }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                N("${index + 1}", c.fg, Modifier.width(22.dp), strong = true)
                RouterField(slot.roles, { value -> ctl.planDraft = draft.editSlot(slot.id) { it.copy(roles = value) } }, Modifier.weight(1f), label = if (index == 0) "Roles · comma-separated" else null)
                RouterSelect(slot.kind, listOf("auto" to "Automatic", "router-fixed" to "Router-fixed", "app-fixed" to "App-supplied"), Modifier.weight(1f), label = if (index == 0) "Selection" else null) { kind -> ctl.planDraft = draft.editSlot(slot.id) { it.copy(kind = kind) } }
                RouterButton("Filters", { editingSlot = if (editingSlot == slot.id) null else slot.id }, selected = editingSlot == slot.id)
            }
            T("${slot.id} · ${if (slot.useDefaults) "shared defaults" else "custom filters"}", Ink.type.small, c.fgMuted, Modifier.padding(start = 30.dp))
            if (slot.kind == "router-fixed") {
                Gap(8.dp)
                RouterSelect(slot.seat, ctl.explainRoster?.seats.orEmpty().map { it.seat to it.seat }, label = "Exact router model") { seat -> ctl.planDraft = draft.editSlot(slot.id) { it.copy(seat = seat) } }
                if (ctl.explainRoster?.seats.isNullOrEmpty()) T("Model catalogue unavailable. Retry from Models or Connection.", Ink.type.small, c.amberText)
            } else if (slot.kind == "app-fixed") {
                Gap(8.dp)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    RouterField(slot.executorId, { value -> ctl.planDraft = draft.editSlot(slot.id) { it.copy(executorId = value) } }, Modifier.weight(1f), label = "Registered executor ID")
                    RouterField(slot.seat, { value -> ctl.planDraft = draft.editSlot(slot.id) { it.copy(seat = value) } }, Modifier.weight(1f), label = "Exact provider:model identity")
                }
                Gap(5.dp)
                T("The router checks the operator's executor registration. External cost and capabilities may be unknown.", Ink.type.small, c.fgMuted)
            }
            if (editingSlot == slot.id) {
                Gap(10.dp)
                RouterToggle("Use shared defaults", slot.useDefaults) { use -> ctl.planDraft = draft.editSlot(slot.id) { it.copy(useDefaults = use, filters = if (!use && it.useDefaults) draft.defaults else it.filters) } }
                if (!slot.useDefaults) {
                    Gap(10.dp)
                    RouterSelect(slot.filters.lane, laneOptions, label = "Seat lane") { lane -> ctl.planDraft = draft.editSlot(slot.id) { it.copy(filters = it.filters.copy(lane = lane)) } }
                    Gap(10.dp)
                    RequirementsEditor(slot.filters, ctl.explainRoster?.providers?.map { it.provider }.orEmpty(), { field, form -> ctl.planDraft = draft.editSlot(slot.id) { it.copy(filters = form, estimate = if (field == "maxTokens") it.estimate?.copy(maxTokens = form.maxTokens.toIntOrNull()) else it.estimate) } })
                    slot.estimate?.let { estimate ->
                        Gap(8.dp)
                        T("Imported estimates: ${estimate.promptTokens ?: "unknown"} prompt · ${estimate.answerTokens ?: "unknown"} expected answer · ${estimate.maxTokens ?: "unset"} token cap", Ink.type.small, c.fgMuted)
                    }
                }
            }
        }
        Gap(16.dp)
        HRule()
        Gap(12.dp)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            T("Defaults: ${draft.defaults.lane} · tools ${triPlan(draft.defaults.toolCalls)} · ${draft.defaults.maxUsdPerMtok.takeIf { it.isNotBlank() }?.let { "≤ $$it / M" } ?: "router price policy"}", Ink.type.small, c.fgMuted)
            RouterButton("Edit defaults", { editingDefaults = !editingDefaults }, selected = editingDefaults)
        }
    }
    if (editingDefaults) {
        Gap(12.dp)
        RouterSelect(draft.defaults.lane, laneOptions, label = "Default lane") { ctl.planDraft = draft.copy(defaults = draft.defaults.copy(lane = it), defaultsEdited = true) }
        Gap(10.dp)
        RequirementsEditor(draft.defaults, ctl.explainRoster?.providers?.map { it.provider }.orEmpty(), { _, form -> ctl.planDraft = draft.copy(defaults = form, defaultsEdited = true) })
        Gap(10.dp)
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            RouterField(draft.promptTokens, { ctl.planDraft = draft.copy(promptTokens = it) }, Modifier.weight(1f), label = "Prompt token estimate per seat", numeric = true)
            RouterSelect(draft.policy, listOf("cheapest" to "Cheapest eligible", "free-only" to "Free only"), Modifier.weight(1f), label = "Plan price policy") { ctl.planDraft = draft.copy(policy = it) }
        }
        Gap(8.dp)
        RouterField(draft.consumer, { ctl.planDraft = draft.copy(consumer = it) }, label = "Configured consumer ID")
    }
    Gap(20.dp)
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        T("Different model weights", Ink.type.heading, c.fg, Modifier.weight(1f))
        RouterToggle("All seats", draft.allDistinct) { ctl.planDraft = draft.copy(allDistinct = it) }
    }
    Gap(10.dp)
    UiCard {
        draft.retainedConstraints.forEach { rule ->
            T("${rule.id}: ${rule.slot} differs from ${rule.others.joinToString { it.slot ?: it.reference.orEmpty() }}", Ink.type.small, c.fgMuted)
            Gap(8.dp)
        }
        draft.groups.forEachIndexed { index, group ->
            if (index > 0) { Gap(12.dp); HRule(); Gap(12.dp) }
            Row(verticalAlignment = Alignment.CenterVertically) {
                T(group.id, Ink.type.bodyStrong, c.fg, Modifier.weight(1f))
                RouterButton("Remove", { ctl.planDraft = draft.copy(groups = draft.groups.filterNot { it.id == group.id }) })
            }
            Gap(8.dp)
            SlotChips(draft.slots, group.slots) { id -> ctl.planDraft = draft.copy(groups = draft.groups.map { if (it.id == group.id) it.copy(slots = togglePlan(it.slots, id), custom = true) else it }) }
            Gap(5.dp)
            T("Every selected seat in this group must use different weights.", Ink.type.small, c.fgMuted)
        }
        if (draft.groups.isEmpty()) T("No custom distinctness groups. Add a pair, triple, or any overlapping set.", Ink.type.small, c.fgMuted)
        Gap(12.dp)
        RouterButton("Add group", {
            var n = draft.groups.size + 1
            while (draft.groups.any { it.id == "group-$n" }) n++
            ctl.planDraft = draft.copy(groups = draft.groups + GroupDraft("group-$n"))
        })
    }
    Gap(20.dp)
    T("Different from a reference model", Ink.type.heading)
    Gap(10.dp)
    UiCard {
        draft.referenceRules.forEachIndexed { index, rule ->
            if (index > 0) { Gap(12.dp); HRule(); Gap(12.dp) }
            Row(verticalAlignment = Alignment.CenterVertically) {
                T(rule.id, Ink.type.bodyStrong, c.fg, Modifier.weight(1f))
                RouterButton("Remove", { ctl.planDraft = draft.copy(referenceRules = draft.referenceRules.filterNot { it.id == rule.id }) })
            }
            Gap(8.dp)
            SlotChips(draft.slots, rule.slots) { id -> ctl.planDraft = draft.copy(referenceRules = draft.referenceRules.map { if (it.id == rule.id) it.copy(slots = togglePlan(it.slots, id), custom = true) else it }) }
            Gap(8.dp)
            RouterSelect(rule.reference, draft.references.map { it.id to it.id }, label = "Reference") { id -> ctl.planDraft = draft.copy(referenceRules = draft.referenceRules.map { if (it.id == rule.id) it.copy(reference = id, custom = true) else it }) }
        }
        if (draft.referenceRules.isEmpty()) T("Connect specific seats to a model or the current PR author reference.", Ink.type.small, c.fgMuted)
        Gap(12.dp)
        RouterButton("Add reference rule", {
            val ref = draft.references.firstOrNull()?.id ?: "pr-author"
            val references = if (draft.references.isEmpty()) listOf(ReferenceDraft(ref)) else draft.references
            var n = draft.referenceRules.size + 1
            while (draft.referenceRules.any { it.id == "reference-rule-$n" }) n++
            ctl.planDraft = draft.copy(references = references, referenceRules = draft.referenceRules + ReferenceRuleDraft("reference-rule-$n", reference = ref))
            showContext = true
        })
    }
    Gap(16.dp)
    RouterButton(if (showContext || draft.references.isNotEmpty()) "PR context & reference identities  ▾" else "PR context & reference identities  ▸", { showContext = !showContext }, Modifier.fillMaxWidth())
    if (showContext || draft.references.isNotEmpty()) {
        Gap(10.dp)
        UiCard {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                RouterField(draft.repo, { ctl.planDraft = draft.copy(repo = it) }, Modifier.weight(1f), label = "Repository", placeholder = "owner/repo")
                RouterField(draft.number, { ctl.planDraft = draft.copy(number = it) }, Modifier.width(90.dp), label = "PR number", numeric = true)
            }
            Gap(10.dp)
            RouterField(draft.headSha, { ctl.planDraft = draft.copy(headSha = it) }, label = "Current PR head revision", placeholder = "exact head SHA")
            Gap(8.dp)
            T("References are checked for this exact revision. Branch names and coauthor hints are not model evidence.", Ink.type.small, c.fgMuted)
            draft.references.forEach { ref ->
                Gap(14.dp)
                HRule()
                Gap(12.dp)
                T(ref.id, Ink.type.bodyStrong)
                Gap(8.dp)
                RouterField(ref.models, { value -> ctl.planDraft = draft.copy(references = draft.references.map { if (it.id == ref.id) it.copy(models = value) else it }) }, label = "Author / reference models · comma-separated provider:model IDs", placeholder = "unknown unless provided by the consumer")
                Gap(8.dp)
                RouterField(ref.evidenceId, { value -> ctl.planDraft = draft.copy(references = draft.references.map { if (it.id == ref.id) it.copy(evidenceId = value) else it }) }, label = "Configured / delegated author evidence ID", placeholder = "required to certify PR-author provenance")
                Gap(8.dp)
                RouterField(ref.revision, { value -> ctl.planDraft = draft.copy(references = draft.references.map { if (it.id == ref.id) it.copy(revision = value) else it }) }, label = "Reference revision", placeholder = "current PR head when blank")
                if (ref.revision.isNotBlank() && ref.revision != draft.headSha) { Gap(5.dp); T("Reference revision differs from the current head. The router will report stale evidence.", Ink.type.small, c.amberText) }
            }
            Gap(12.dp)
            RouterButton("Add reference", {
                var n = draft.references.size + 1
                while (draft.references.any { it.id == "reference-$n" }) n++
                ctl.planDraft = draft.copy(references = draft.references + ReferenceDraft("reference-$n"))
            })
        }
    }
    Gap(20.dp)
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        RouterButton(if (ctl.planLoading) "Resolving…" else "Preview seat plan", { ctl.resolvePlan() }, primary = true, enabled = error == null && !ctl.planLoading)
        T("Preview only — no model calls.", Ink.type.small, c.fgMuted, Modifier.weight(1f))
    }
    error?.let { Gap(8.dp); T(it, Ink.type.small, c.amberText) }
}

private val laneOptions = listOf("interactive" to "Interactive", "background" to "Background", "batch" to "Batch")
private fun togglePlan(values: List<String>, value: String) = if (value in values) values - value else values + value
private fun triPlan(value: Boolean?) = when (value) { true -> "required"; false -> "excluded"; null -> "any" }

@Composable
private fun SlotChips(slots: List<SlotDraft>, selected: List<String>, onToggle: (String) -> Unit) {
    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        slots.forEachIndexed { index, slot -> RouterButton("${index + 1} · ${slot.id}", { onToggle(slot.id) }, selected = slot.id in selected) }
    }
}

@Composable
private fun PlanEvidence(ctl: AppController) {
    val c = Ink.colors
    T("Plan preview", Ink.type.title)
    Gap(6.dp)
    T("The router resolves the complete plan from one snapshot.", Ink.type.small, c.fgMuted)
    Gap(18.dp)
    ctl.planError?.let { UiCard { StatusChip("Preview unavailable", ChipKind.Warning); Gap(10.dp); T(it, Ink.type.body, c.amberText) } }
    val plan = ctl.planResult
    if (plan == null) {
        if (ctl.planError == null) UiCard { T(if (ctl.planLoading) "Resolving all seats and rules…" else "Your complete plan will appear here.", Ink.type.body, c.fgMuted); Gap(10.dp); T("Unknown identities and stale author references cannot certify a diversity rule.", Ink.type.small, c.fgMuted) }
        return
    }
    if (runCatching { ctl.planDraft.request() }.getOrNull() != ctl.planSubmittedRequest) {
        StatusChip("Draft changed — preview the plan again", ChipKind.Warning)
        Gap(12.dp)
    }
    UiCard {
        StatusChip(if (plan.outcome == "RESOLVED") "${plan.assignments.size} seats resolved" else plan.outcome.replace('_', ' '), if (plan.outcome == "RESOLVED") ChipKind.Positive else ChipKind.Warning)
        Gap(10.dp)
        T(plan.because, Ink.type.body, c.fgMuted)
        if (plan.outcome == "RESOLVED") {
            Gap(12.dp)
            StatusChip("Required groups satisfied", ChipKind.Positive)
        }
        Gap(18.dp)
        T("Assigned seats", Ink.type.heading)
        val appWeights = plan.assignments.filter { it.binding.kind == "app-fixed" }.mapNotNull { it.weightsId }.toSet()
        val colors = listOf(c.accent, c.success, Color(0xFF8357C5))
        val weightColors = plan.assignments.mapNotNull { it.weightsId }.distinct().filter { it !in appWeights }.mapIndexed { index, weight -> weight to colors[index % colors.size] }.toMap()
        plan.assignments.forEach { assignment ->
            Gap(12.dp)
            HRule()
            Gap(12.dp)
            Row(verticalAlignment = Alignment.CenterVertically) {
                val color = when { assignment.weightsId == null -> c.amberText; assignment.weightsId in appWeights -> c.fgFaint; else -> weightColors[assignment.weightsId] ?: c.fgFaint }
                Box(Modifier.width(3.dp).height(84.dp).background(color))
                HGap(10.dp)
                Column(Modifier.weight(1f)) {
                    T(assignment.slotId + " · " + assignment.roles.ifEmpty { listOfNotNull(assignment.role) }.joinToString(", "), Ink.type.bodyStrong)
                    Gap(4.dp)
                    T(assignment.seat ?: assignment.binding.modelRef ?: "UNKNOWN model", Ink.type.body, c.fg)
                    T("Weights: ${assignment.weightsId ?: "UNKNOWN"}", Ink.type.smallStrong, color)
                    assignment.identityEvidenceId?.let { T("Identity evidence: $it", Ink.type.small, c.fgMuted) }
                    T("Estimated: ${assignment.estimatedUsd?.let { "$$it USD" } ?: "UNKNOWN"} · ${assignment.lane ?: "lane unknown"}", Ink.type.small, c.fgMuted)
                }
                HGap(8.dp)
                StatusChip(if (assignment.binding.kind == "app-fixed") "App-supplied" else if (plan.assignments.any { it.slotId != assignment.slotId && it.weightsId != null && it.weightsId == assignment.weightsId }) "Weight reused" else "Assigned", ChipKind.Neutral)
            }
        }
        Gap(18.dp)
        T("Constraint evidence", Ink.type.heading)
        if (plan.constraintEvidence.isEmpty()) { Gap(8.dp); T("No distinctness constraints reported.", Ink.type.small, c.fgMuted) }
        plan.constraintEvidence.forEach { evidence ->
            Gap(12.dp)
            StatusChip("${evidence.id}: ${when (evidence.satisfied) { true -> "satisfied"; false -> "not satisfied"; null -> "unknown" }}", when (evidence.satisfied) { true -> ChipKind.Positive; else -> ChipKind.Warning })
            evidence.pairs.forEach { pair ->
                Gap(5.dp)
                T("${pair.left.slot ?: pair.left.reference ?: "?"} ↔ ${pair.right.slot ?: pair.right.reference ?: "?"}: ${pair.leftWeightsId ?: "UNKNOWN"} / ${pair.rightWeightsIds.joinToString(", ").ifBlank { "UNKNOWN" }}", Ink.type.small, c.fgMuted)
            }
        }
        plan.unknown.forEach { missing -> Gap(10.dp); T("Unknown ${missing.slotId ?: missing.referenceId ?: missing.seat.orEmpty()}: ${missing.because}", Ink.type.small, c.amberText); if (missing.facts.isNotEmpty()) T("Missing facts: ${missing.facts.joinToString(", ").replace('_', ' ')}", Ink.type.small, c.amberText) }
        if (plan.references.isNotEmpty()) {
            Gap(14.dp)
            T("Reference provenance", Ink.type.heading)
            plan.references.forEach { (id, evidence) ->
                Gap(10.dp)
                T(id, Ink.type.bodyStrong)
                val obj = evidence as? JsonObject
                if (obj == null) T(Fmt.jsonValue(evidence), Ink.type.small, c.fgMuted)
                else {
                    T("Provenance: ${obj["provenance"]?.let { Fmt.jsonValue(it).replace('-', ' ') } ?: "UNKNOWN"}", Ink.type.small, c.fgMuted)
                    obj["revision"]?.let { T("Reference revision: ${Fmt.jsonValue(it)}", Ink.type.small, c.fgMuted) }
                    val models = obj["models"] as? JsonArray
                    models?.forEach { model ->
                        val facts = model as? JsonObject
                        T("Weights: ${facts?.get("weights_id")?.let(Fmt::jsonValue) ?: "UNKNOWN"}", Ink.type.small, c.fgMuted)
                        facts?.get("identity_evidence_id")?.let { T("Identity evidence: ${Fmt.jsonValue(it)}", Ink.type.small, c.fgMuted) }
                    }
                    obj["unknown"]?.let { if (it is JsonArray && it.isNotEmpty()) T("Missing facts: ${it.joinToString { fact -> Fmt.jsonValue(fact).replace('_', ' ') }}", Ink.type.small, c.amberText) }
                }
            }
        }
        Gap(16.dp)
        HRule()
        Gap(12.dp)
        T("Provider aliases of the same weights count together. Different weights do not prove independent judgment.", Ink.type.small, c.fgMuted)
        Gap(8.dp)
        T("Estimate scope: one invocation per seat. External app costs can remain unknown.", Ink.type.small, c.fgMuted)
        plan.estimates.forEach { (name, amount) -> Gap(4.dp); T("${name.replace('_', ' ')}: ${Fmt.jsonValue(amount)}", Ink.type.small, c.fgMuted) }
        plan.planId?.let { Gap(8.dp); T("Plan $it · version ${plan.version ?: "UNKNOWN"}", Ink.type.small, c.fgMuted) }
    }
    if (plan.considered.isNotEmpty()) {
        Gap(16.dp)
        UiCard {
            T("Models considered", Ink.type.heading)
            plan.considered.take(30).forEach { candidate ->
                Gap(10.dp)
                HRule()
                Gap(8.dp)
                T("${candidate.slotId} · ${candidate.seat ?: candidate.executorId ?: "UNKNOWN"}", Ink.type.bodyStrong)
                Gap(4.dp)
                StatusChip(candidate.verdict, when (candidate.verdict) { "QUALIFIES" -> ChipKind.Positive; "EXCLUDED" -> ChipKind.Excluded; else -> ChipKind.Warning })
                Gap(4.dp)
                T(candidate.because, Ink.type.small, c.fgMuted)
            }
            if (plan.considered.size > 30) { Gap(8.dp); T("Showing the first 30 of ${plan.considered.size} reported considerations.", Ink.type.small, c.fgMuted) }
        }
    }
}
