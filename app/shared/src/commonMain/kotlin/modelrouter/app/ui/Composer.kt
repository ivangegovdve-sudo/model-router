package modelrouter.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Popup
import androidx.compose.ui.window.PopupProperties
import kotlinx.serialization.json.JsonNull
import modelrouter.app.*

@Composable
fun RouteTabs(ctl: AppController) {
    Row(Modifier.fillMaxWidth().padding(horizontal = 24.dp, vertical = 10.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        RouterButton("Single request", { ctl.screen = Screen.Explain }, selected = ctl.screen == Screen.Explain)
        RouterButton("Seat plan", { ctl.screen = Screen.SeatPlan }, selected = ctl.screen == Screen.SeatPlan)
    }
    HRule()
}

@Composable
fun ComposerLayout(sidebar: @Composable () -> Unit, editor: @Composable () -> Unit, evidence: @Composable () -> Unit) {
    BoxWithConstraints(Modifier.fillMaxSize()) {
        if (maxWidth >= 1200.dp) {
            Row(Modifier.fillMaxSize()) {
                Column(Modifier.width(224.dp).fillMaxHeight().verticalScroll(rememberScrollState()).padding(18.dp)) { sidebar() }
                VRule()
                Column(Modifier.weight(1.15f).fillMaxHeight().verticalScroll(rememberScrollState()).padding(24.dp)) { editor() }
                VRule()
                Column(Modifier.weight(1f).fillMaxHeight().verticalScroll(rememberScrollState()).padding(24.dp)) { evidence() }
            }
        } else {
            Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp)) {
                sidebar()
                Gap(24.dp)
                editor()
                Gap(28.dp)
                HRule()
                Gap(24.dp)
                evidence()
            }
        }
    }
}

@Composable
fun UiCard(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Column(modifier.fillMaxWidth().background(Ink.colors.surface, Shapes.corner).border(1.dp, Ink.colors.rule, Shapes.corner).padding(16.dp), content = content)
}

@Composable
fun GoalCard(name: String, description: String, selected: Boolean, onClick: () -> Unit) {
    val c = Ink.colors
    Column(Modifier.fillMaxWidth().background(if (selected) c.accentBg else c.bg, Shapes.corner)
        .border(if (selected) 1.dp else 0.dp, if (selected) c.accent.copy(alpha = .35f) else c.bg, Shapes.corner)
        .clickable(onClick = onClick).padding(horizontal = 14.dp, vertical = 18.dp)) {
        T(name, Ink.type.bodyStrong, if (selected) c.accent else c.fg)
        Gap(5.dp)
        T(description, Ink.type.small, c.fgMuted)
    }
    Gap(8.dp)
}

@Composable
fun RouterSelect(value: String, options: List<Pair<String, String>>, modifier: Modifier = Modifier, label: String? = null, onSelect: (String) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    var search by remember { mutableStateOf("") }
    val c = Ink.colors
    Box(modifier) {
        Column {
            label?.let { T(it, Ink.type.smallStrong, c.fgMuted); Gap(4.dp) }
            RouterButton((options.firstOrNull { it.first == value }?.second ?: value.ifBlank { "Choose…" }) + "  ▾", { expanded = true; search = "" }, Modifier.fillMaxWidth(), selected = expanded)
        }
        if (expanded) Popup(alignment = Alignment.TopStart, offset = IntOffset(0, if (label == null) 44 else 68), onDismissRequest = { expanded = false }, properties = PopupProperties(focusable = true)) {
            Column(Modifier.width(340.dp).heightIn(max = 360.dp).background(c.surface, Shapes.corner).border(1.dp, c.ruleStrong, Shapes.corner).padding(6.dp).verticalScroll(rememberScrollState())) {
                if (options.size > 12) { RouterField(search, { search = it }, placeholder = "Search models"); Gap(6.dp) }
                val matching = options.filter { it.second.contains(search, true) }
                matching.take(80).forEach { option ->
                    RouterButton(option.second, { onSelect(option.first); expanded = false }, Modifier.fillMaxWidth(), selected = option.first == value)
                    Gap(3.dp)
                }
                if (matching.size > 80) T("Refine search to see all ${matching.size} matches.", Ink.type.small, c.fgMuted, Modifier.padding(6.dp))
                if (matching.isEmpty()) T("No matching options.", Ink.type.small, c.fgMuted, Modifier.padding(6.dp))
            }
        }
    }
}

@Composable
fun SelectionRow(label: String, options: List<String>, selected: List<String>, onSelect: (String) -> Unit) {
    T(label, Ink.type.smallStrong, Ink.colors.fgMuted)
    Gap(5.dp)
    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        options.forEach { value -> RouterButton(value.replace('_', ' ').replaceFirstChar { it.uppercase() }, { onSelect(value) }, selected = value in selected) }
    }
}

private fun toggle(values: List<String>, value: String) = if (value in values) values - value else values + value
private fun tri(value: Boolean?) = when (value) { true -> "required"; false -> "excluded"; null -> "any" }
private fun triValue(value: String) = when (value) { "required" -> true; "excluded" -> false; else -> null }

@Composable
fun RequirementsEditor(form: ExplainForm, providers: List<String>, onChange: (String, ExplainForm) -> Unit, includeBudget: Boolean = true) {
    val c = Ink.colors
    var advanced by remember { mutableStateOf(false) }
    UiCard {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            T("Custom filters", Ink.type.heading, c.fg, Modifier.weight(1f))
            RouterToggle("Use filters", form.filtersEnabled) { onChange("filtersEnabled", form.copy(filtersEnabled = it)) }
        }
        if (!form.filtersEnabled) { Gap(10.dp); T("Custom filters are paused. Your edits are preserved.", Ink.type.small, c.fgMuted) }
        else {
            Gap(16.dp)
            SelectionRow("Model types · any selected type", listOf("text", "reasoning", "vision"), form.modelTypes) { onChange("modelTypes", form.copy(modelTypes = toggle(form.modelTypes, it))) }
            Gap(14.dp)
            HRule()
            Gap(14.dp)
            SelectionRow("Required capabilities", listOf("tools", "reasoning", "structured_output", "editing"), form.capabilities) { onChange("capabilities", form.copy(capabilities = toggle(form.capabilities, it))) }
            Gap(12.dp)
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                RouterSelect(tri(form.toolCalls), listOf("any" to "Any", "required" to "Required", "excluded" to "Excluded"), Modifier.weight(1f), "Tool calls") { onChange("toolCalls", form.copy(toolCalls = triValue(it))) }
                RouterSelect(tri(form.editable), listOf("any" to "Any", "required" to "Required", "excluded" to "Excluded"), Modifier.weight(1f), "Code / content editing") { onChange("editable", form.copy(editable = triValue(it))) }
            }
            Gap(14.dp)
            HRule()
            Gap(14.dp)
            SelectionRow("Thinking level", listOf("any", "none", "low", "medium", "high"), listOf(form.thinkingLevel ?: "any")) { onChange("thinkingLevel", form.copy(thinkingLevel = it.takeUnless { v -> v == "any" })) }
            Gap(14.dp)
            HRule()
            Gap(14.dp)
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Column(Modifier.weight(1f)) { T("Price ceiling", Ink.type.bodyStrong, c.fg); T("Maximum USD per 1M tokens.", Ink.type.small, c.fgMuted) }
                RouterToggle("Limit", form.maxUsdPerMtok.isNotBlank()) { onChange("maxUsdPerMtok", form.copy(maxUsdPerMtok = if (it) "2.00" else "")) }
                RouterField(form.maxUsdPerMtok, { onChange("maxUsdPerMtok", form.copy(maxUsdPerMtok = it)) }, Modifier.width(100.dp), placeholder = "any", numeric = true, decimal = true)
            }
            Gap(14.dp)
            HRule()
            Gap(10.dp)
            RouterButton(if (advanced) "Hide providers, context & answer budget" else "Providers, context & answer budget  ▸", { advanced = !advanced }, Modifier.fillMaxWidth())
            if (advanced) {
                Gap(12.dp)
                SelectionRow("Providers · any when none selected", providers, form.providers) { onChange("providers", form.copy(providers = toggle(form.providers, it))) }
                if (providers.isEmpty()) T("Provider options load from the router.", Ink.type.small, c.fgMuted)
                Gap(12.dp)
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    RouterField(form.minContextTokens, { onChange("minContextTokens", form.copy(minContextTokens = it)) }, Modifier.weight(1f), label = "Minimum context tokens", placeholder = "any", numeric = true)
                    if (includeBudget) RouterField(form.maxTokens, { onChange("maxTokens", form.copy(maxTokens = it)) }, Modifier.weight(1f), label = "Answer budget tokens", placeholder = "router default", numeric = true)
                }
                Gap(6.dp)
                T("Missing capability or price evidence stays unknown.", Ink.type.small, c.fgMuted)
            }
        }
    }
}

@Composable
fun ExplainScreen(ctl: AppController) {
    LaunchedEffect(ctl.settings) { if (ctl.explainRoster == null && ctl.explainRosterError == null) ctl.loadExplainRoster() }
    Column(Modifier.fillMaxSize()) {
        RouteTabs(ctl)
        ComposerLayout(sidebar = { SingleGoals(ctl) }, editor = { SingleEditor(ctl) }, evidence = { SingleEvidence(ctl) })
    }
}

@Composable
private fun SingleGoals(ctl: AppController) {
    T("Goal templates", Ink.type.heading)
    Gap(18.dp)
    BoxWithConstraints {
        val compactRail = maxWidth <= 350.dp
        Column {
        if (!compactRail) {
            RouterSelect(ctl.selectedGoal, GoalTemplates.map { it.id to it.name }, label = "Start with a goal", onSelect = ctl::chooseGoal)
            Gap(8.dp)
            T(GoalTemplates.first { it.id == ctl.selectedGoal }.description, Ink.type.small, Ink.colors.fgMuted)
        } else GoalTemplates.forEach { goal -> GoalCard(goal.name, goal.description, ctl.selectedGoal == goal.id) { ctl.chooseGoal(goal.id) } }
        }
    }
    Gap(22.dp)
    T("Templates suggest defaults. Your text and custom filters stay under your control.", Ink.type.small, Ink.colors.fgMuted)
}

@Composable
private fun SingleEditor(ctl: AppController) {
    val c = Ink.colors
    val form = ctl.explainForm()
    var editFilters by remember { mutableStateOf(true) }
    val validation = runCatching { form.request() }.exceptionOrNull()?.message
    T("What should the model handle?", Ink.type.title)
    Gap(5.dp)
    T("Start with a goal, then refine your request.", Ink.type.body, c.fgMuted)
    Gap(16.dp)
    FlowRow(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        StatusChip(GoalTemplates.first { it.id == ctl.selectedGoal }.name, ChipKind.Selected)
        if (ctl.goalEdited.isNotEmpty()) T("Customized", Ink.type.small, c.fgMuted)
    }
    Gap(18.dp)
    Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
        RouterSelect(form.seat ?: "auto", listOf("auto" to "Automatic") + ctl.explainRoster?.seats.orEmpty().map { it.seat to it.seat }, Modifier.weight(1f), "Model") { ctl.setExplainForm(form.copy(seat = it.takeUnless { v -> v == "auto" }), "seat") }
        RouterSelect(form.lane, listOf("interactive" to "Interactive", "background" to "Background", "batch" to "Batch"), Modifier.weight(1f), "Lane") { ctl.setExplainForm(form.copy(lane = it), "lane") }
    }
    ctl.explainRosterError?.let { Gap(8.dp); T("Model catalogue unavailable: $it", Ink.type.small, c.amberText); RouterButton("Retry catalogue", { ctl.loadExplainRoster() }) }
    Gap(18.dp)
    RouterField(form.prompt, { ctl.setExplainForm(form.copy(prompt = it), "prompt") }, label = "Your request", singleLine = false, minLines = 5)
    Gap(8.dp)
    T("${form.prompt.length} characters · answer budget ${form.maxTokens.ifBlank { "router default" }} tokens", Ink.type.small, c.fgMuted)
    Gap(14.dp)
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        StatusChip("Thinking: ${form.thinkingLevel ?: "any"}", ChipKind.Neutral)
        StatusChip("Tools: ${tri(form.toolCalls)}", ChipKind.Neutral)
        StatusChip("Price: ${form.maxUsdPerMtok.takeIf { it.isNotBlank() }?.let { "≤ $$it / M" } ?: "router policy"}", ChipKind.Neutral)
        RouterButton("Edit filters", { editFilters = !editFilters }, selected = editFilters)
    }
    Gap(16.dp)
    if (editFilters) {
        RequirementsEditor(form, ctl.explainRoster?.providers?.map { it.provider }?.distinct().orEmpty(), { field, next -> ctl.setExplainForm(next, field) })
        Gap(10.dp)
        RouterButton("Reset to goal defaults", { ctl.resetGoalFilters() })
        Gap(10.dp)
        RouterToggle("Include a sample lookup tool", form.includeTool) { ctl.setExplainForm(form.copy(includeTool = it), "includeTool") }
    }
    Gap(20.dp)
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        RouterButton(if (ctl.explainLoading) "Previewing…" else "Preview route", { ctl.runExplain() }, primary = true, enabled = validation == null && !ctl.explainLoading)
        T("Preview only — no model call.", Ink.type.small, c.fgMuted, Modifier.weight(1f))
    }
    validation?.let { Gap(8.dp); T(it, Ink.type.small, c.amberText) }
    form.seat?.let { chosen -> ctl.explainRoster?.seats?.firstOrNull { it.seat == chosen }?.let { Gap(18.dp); SeatCapabilities(it) } }
}

@Composable
private fun SingleEvidence(ctl: AppController) {
    val c = Ink.colors
    T("Route preview", Ink.type.title)
    Gap(5.dp)
    T("The router's last result, based on the submitted requirements.", Ink.type.small, c.fgMuted)
    Gap(18.dp)
    ctl.explainError?.let { UiCard { StatusChip("Preview unavailable", ChipKind.Warning); Gap(8.dp); T(it, Ink.type.body, c.amberText) } }
    val choice = ctl.explainResult?.choice
    if (choice == null) {
        if (ctl.explainError == null) UiCard { T(if (ctl.explainLoading) "Reading router evidence…" else "Your route preview will appear here.", Ink.type.body, c.fgMuted); Gap(8.dp); T("Choose a goal, edit your requirements, then preview the route.", Ink.type.small, c.fgMuted) }
        return
    }
    if (ctl.explainResultRevision != ctl.explainRevision) {
        StatusChip("Draft changed — preview route again", ChipKind.Warning)
        Gap(12.dp)
    }
    UiCard {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            T(choice.seat ?: "No eligible model", Ink.type.heading, c.fg, Modifier.weight(1f))
            HGap(8.dp)
            StatusChip(if (choice.isRoute) "Selected" else choice.outcome, if (choice.isRoute) ChipKind.Selected else ChipKind.Warning)
        }
        Gap(16.dp)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(24.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Column { T("Estimated request cost", Ink.type.small, c.fgMuted); N(choice.expectedUsd?.let { Fmt.money(it) } ?: "UNKNOWN", c.fg, strong = true, large = true) }
            Column {
                val level = choice.confidence?.level ?: "unknown"
                StatusChip("${level.replaceFirstChar { it.uppercase() }} evidence", when (level) { "high" -> ChipKind.Selected; "medium", "low", "unknown" -> ChipKind.Warning; else -> ChipKind.Neutral })
                Gap(5.dp)
                T("Evidence strength, not success probability.", Ink.type.small, c.fgMuted)
            }
        }
        Gap(14.dp)
        HRule()
        Gap(12.dp)
        val facts = choice.facts
        FlowRow(horizontalArrangement = Arrangement.spacedBy(18.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            LabeledText("Estimated prompt", facts.promptTokens?.let { "$it tokens" } ?: "UNKNOWN")
            LabeledText("Answer budget", facts.maxTokens?.let { "$it tokens" } ?: "router default")
            LabeledText("Context needed", facts.requestFeatures["context_needed_tokens"]?.let { Fmt.jsonValue(it) + " tokens" } ?: "UNKNOWN")
            LabeledText("Requested lane", facts.lane ?: "not reported")
        }
    }
    Gap(16.dp)
    UiCard {
        T("Why this model", Ink.type.heading)
        Gap(12.dp)
        T(choice.because, Ink.type.body, c.fgMuted)
        choice.confidence?.evidence?.forEach { line -> Gap(10.dp); T(line.replaceFirstChar { it.uppercase() }, Ink.type.body, c.accent) }
        Gap(10.dp)
        T("Emission probes show content was returned. They do not measure task competence.", Ink.type.small, c.fgMuted)
        if (choice.unknown.isNotEmpty()) { Gap(8.dp); T("Unknown: ${choice.unknown.joinToString(", ")}", Ink.type.small, c.amberText) }
    }
    Gap(16.dp)
    UiCard { T("Models considered", Ink.type.heading); Gap(12.dp); if (choice.considered.isEmpty()) T("No candidates reported.", Ink.type.small, c.fgMuted) else ConsideredTable(choice) }
    Gap(16.dp)
    ChoiceTelemetry(choice)
}
