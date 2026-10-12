package modelrouter.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import modelrouter.app.Candidate
import modelrouter.app.Fmt
import modelrouter.app.ProviderState
import modelrouter.app.RouterSettings

// ---- Roster ---------------------------------------------------------------------------------

@Composable
fun RosterScreen(ctl: AppController) {
    LaunchedEffect(ctl.settings) { if (ctl.roster == null && ctl.rosterError == null) ctl.loadRoster() }
    val c = Ink.colors
    val ty = Ink.type
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            T("Roster", ty.title, c.fg, Modifier.weight(1f))
            RouterToggle("measured only", ctl.measuredOnly) { ctl.measuredOnly = it; ctl.loadRoster() }
            HGap(8.dp)
            RouterButton("Refresh", { ctl.loadRoster() })
        }
        Gap(10.dp)
        if (ctl.rosterLoading && ctl.roster == null) T("Loading roster", ty.body, c.fgMuted)
        ctl.rosterError?.let { err ->
            T("Router unreachable: $err", ty.bodyStrong, c.amberText)
            Gap(8.dp)
            RouterButton("Retry", { ctl.loadRoster() })
        }
        ctl.roster?.let { r ->
            T("Providers", ty.heading, c.fg)
            Gap(6.dp)
            ProviderTable(r.providers, r.dashboard)
            Gap(18.dp)
            Row(verticalAlignment = Alignment.CenterVertically) {
                T("Seats", ty.heading, c.fg)
                HGap(8.dp)
                N("${r.seats.size}", c.fgMuted)
            }
            Gap(6.dp)
            if (r.seats.isEmpty()) T("No seats match this filter.", ty.small, c.fgMuted)
            else SeatsTable(r.seats)
        }
    }
}

@Composable
private fun ProviderTable(providers: List<ProviderState>, dashboard: Map<String, kotlinx.serialization.json.JsonElement>) {
    val c = Ink.colors
    val ty = Ink.type
    RouterTable {
        Row(Modifier.fillMaxWidth().background(c.surfaceSunk).padding(horizontal = 10.dp, vertical = 6.dp)) {
            T("provider", ty.smallStrong, c.fgMuted, Modifier.weight(1f))
            T("state", ty.smallStrong, c.fgMuted, Modifier.weight(0.8f))
            T("detail", ty.smallStrong, c.fgMuted, Modifier.weight(1.4f))
            T("models", ty.smallStrong, c.fgMuted, Modifier.weight(0.6f))
            T("dashboard", ty.smallStrong, c.fgMuted, Modifier.weight(1.4f))
        }
        providers.forEachIndexed { i, p ->
            HRule()
            Row(Modifier.fillMaxWidth().padding(horizontal = 10.dp, vertical = 7.dp), verticalAlignment = Alignment.CenterVertically) {
                T(p.provider, ty.bodyStrong, c.fg, Modifier.weight(1f))
                Box(Modifier.weight(0.8f)) {
                    StatusChip(p.state.ifBlank { "UNKNOWN" }, if (p.ok) ChipKind.Positive else ChipKind.Warning)
                }
                T(p.detail.ifBlank { "-" }, ty.small, c.fgMuted, Modifier.weight(1.4f), maxLines = 2)
                N(p.models?.toString() ?: "?", c.fg, Modifier.weight(0.6f))
                T(dashboard[p.provider]?.let { Fmt.jsonValue(it) } ?: "not carried", ty.small, c.fgMuted, Modifier.weight(1.4f), maxLines = 2)
            }
        }
    }
}

@Composable
private fun SeatsTable(seats: List<Candidate>) {
    val c = Ink.colors
    val ty = Ink.type
    RouterTable {
        Row(Modifier.fillMaxWidth().background(c.surfaceSunk).padding(horizontal = 10.dp, vertical = 6.dp)) {
            T("seat", ty.smallStrong, c.fgMuted, Modifier.weight(1.8f))
            T("emits", ty.smallStrong, c.fgMuted, Modifier.weight(1.1f))
            T("min tokens", ty.smallStrong, c.fgMuted, Modifier.weight(0.8f))
            T("measured $/M", ty.smallStrong, c.fgMuted, Modifier.weight(0.9f))
            T("list input $/M", ty.smallStrong, c.fgMuted, Modifier.weight(0.9f))
            T("latency", ty.smallStrong, c.fgMuted, Modifier.weight(0.7f))
        }
        seats.forEachIndexed { i, s ->
            HRule()
            val unavailable = s.available == false || (s.providerState.isNotBlank() && !s.providerState.equals("OK", true))
            Row(
                Modifier.fillMaxWidth()
                    .then(if (unavailable) Modifier.hatched(c.amberHatch, c.amber.copy(alpha = 0.28f)) else Modifier)
                    .padding(horizontal = 10.dp, vertical = 7.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(Modifier.weight(1.8f)) {
                    T(s.seat, ty.body, c.fg, maxLines = 1)
                    if (unavailable) T(s.providerState + (s.providerDetail.takeIf { it.isNotBlank() }?.let { ": $it" } ?: ""), ty.small, c.amberText, maxLines = 1)
                    else if (s.available == null) T("availability UNKNOWN", ty.small, c.amberText)
                }
                T(s.emits ?: "UNKNOWN", ty.small, if (s.emits == null) c.amberText else c.fgMuted, Modifier.weight(1.1f), maxLines = 1)
                N(s.minMaxTokens?.toString() ?: "?", c.fgMuted, Modifier.weight(0.8f))
                val measured = Fmt.perMtok(s.measuredUsdPerMtok)
                Box(Modifier.weight(0.9f)) { if (measured.unknown) T("UNKNOWN", ty.smallStrong, c.amberText) else N(measured.text, c.fg, strong = true) }
                val listPrice = s.listPrompt
                Box(Modifier.weight(0.9f)) {
                    val lp = Fmt.perMtok(listPrice)
                    if (lp.unknown) T("UNKNOWN", ty.small, c.amberText) else N(lp.text, c.fgMuted)
                }
                N(Fmt.seconds(s.latencyS), c.fgMuted, Modifier.weight(0.7f))
            }
        }
    }
}

// ---- Explain --------------------------------------------------------------------------------

@Composable
fun ExplainScreen(ctl: AppController) {
    LaunchedEffect(ctl.settings) { if (ctl.explainRoster == null && ctl.explainRosterError == null) ctl.loadExplainRoster() }
    BoxWithConstraints(Modifier.fillMaxSize()) {
        if (maxWidth >= 1050.dp) {
            Row(Modifier.fillMaxSize()) {
                ExplainFormPanel(ctl, Modifier.width(540.dp).fillMaxHeight().verticalScroll(rememberScrollState()).padding(16.dp))
                VRule()
                Column(Modifier.weight(1f).fillMaxHeight().verticalScroll(rememberScrollState()).padding(16.dp)) {
                    ExplainOutcome(ctl)
                }
            }
        } else {
            Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
                ExplainFormPanel(ctl, Modifier.fillMaxWidth())
                Gap(16.dp)
                HRule()
                Gap(14.dp)
                ExplainOutcome(ctl)
            }
        }
    }
}

@Composable
private fun ExplainFormPanel(ctl: AppController, modifier: Modifier) {
    val c = Ink.colors
    val ty = Ink.type
    var choosingSeat by remember { mutableStateOf(false) }
    var seatSearch by remember { mutableStateOf("") }
    val validation = runCatching { ctl.explainForm().request() }.exceptionOrNull()?.message
    Column(modifier) {
        T("Explain a route", ty.title, c.fg)
        Gap(4.dp)
        T("Preview the router's choice, price and evidence. No model call is made.", ty.small, c.fgMuted)
        Gap(14.dp)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            RouterButton(ctl.explainSeat ?: "Auto · ${ctl.explainLane}", { choosingSeat = !choosingSeat }, selected = choosingSeat)
            if (ctl.explainSeat != null) RouterButton("Use auto", { ctl.explainSeat = null })
        }
        Gap(8.dp)
        SelectionRow("Routing lane", listOf("interactive", "background", "batch"), listOf(ctl.explainLane)) { ctl.explainLane = it }
        if (ctl.explainSeat != null) {
            Gap(4.dp)
            T("The selected seat is evaluated against the same requirements.", ty.small, c.fgMuted)
        }
        if (choosingSeat) {
            Gap(10.dp)
            Column(Modifier.fillMaxWidth().background(c.surfaceSunk).border(1.dp, c.rule).padding(10.dp)) {
                RouterField(seatSearch, { seatSearch = it }, label = "Select a seat from the router roster", placeholder = "Search provider or model")
                Gap(8.dp)
                if (ctl.explainRosterLoading) T("Loading model catalogue…", ty.small, c.fgMuted)
                ctl.explainRosterError?.let {
                    T("Catalogue unavailable: $it", ty.small, c.amberText)
                    Gap(6.dp)
                    RouterButton("Retry catalogue", { ctl.loadExplainRoster() })
                }
                val seats = ctl.explainRoster?.seats.orEmpty().filter { it.seat.contains(seatSearch, ignoreCase = true) }
                Column(Modifier.fillMaxWidth().heightIn(max = 260.dp).verticalScroll(rememberScrollState())) {
                    RouterButton("Auto · let the router choose", { ctl.explainSeat = null; choosingSeat = false }, Modifier.fillMaxWidth(), selected = ctl.explainSeat == null)
                    seats.take(80).forEach { seat ->
                        Gap(4.dp)
                        RouterButton(seat.seat, { ctl.explainSeat = seat.seat; choosingSeat = false }, Modifier.fillMaxWidth(), selected = ctl.explainSeat == seat.seat)
                    }
                    if (seats.size > 80) { Gap(6.dp); T("${seats.size} seats match. Refine search to see more.", ty.small, c.fgMuted) }
                    if (ctl.explainRoster != null && seats.isEmpty()) { Gap(6.dp); T("No seats match this search.", ty.small, c.fgMuted) }
                }
            }
        }
        ctl.explainRoster?.seats?.firstOrNull { it.seat == ctl.explainSeat }?.let { seat ->
            Gap(8.dp)
            SeatCapabilities(seat)
        }
        Gap(14.dp)
        RouterField(ctl.explainPrompt, { ctl.explainPrompt = it }, label = "Request context", placeholder = "What would you ask the router?", singleLine = false, minLines = 3)
        Gap(10.dp)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            RouterField(ctl.explainMaxTokens, { ctl.explainMaxTokens = it }, Modifier.widthIn(max = 220.dp), label = "Answer budget · max tokens", placeholder = "router default", numeric = true)
            Column {
                T("Request tools", ty.smallStrong, c.fgMuted)
                Gap(4.dp)
                RouterToggle("Include a sample lookup tool", ctl.explainNeedsTools) { ctl.explainNeedsTools = it }
            }
        }
        Gap(14.dp)
        HRule()
        Gap(12.dp)
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            T("Model requirements", ty.heading, c.fg, Modifier.weight(1f))
            RouterToggle(if (ctl.explainFiltersEnabled) "Filters on" else "Filters off", ctl.explainFiltersEnabled) { ctl.explainFiltersEnabled = it }
        }
        Gap(6.dp)
        T(if (ctl.explainFiltersEnabled) "Requirements go to the router. Missing capability evidence stays UNKNOWN." else "Saved filters are paused. The router's normal policy still applies.", ty.small, c.fgMuted)
        if (ctl.explainFiltersEnabled) {
            Gap(10.dp)
            val providers = ctl.explainRoster?.providers?.map { it.provider }?.distinct().orEmpty()
            if (providers.isNotEmpty()) SelectionRow("Providers · any when none selected", providers, ctl.explainProviders) { ctl.explainProviders = toggleItem(ctl.explainProviders, it) }
            else T("Provider filters load with the model catalogue.", ty.small, c.fgMuted)
            Gap(10.dp)
            SelectionRow("Model types · any when none selected", listOf("text", "reasoning", "vision"), ctl.explainModelTypes) { ctl.explainModelTypes = toggleItem(ctl.explainModelTypes, it) }
            Gap(10.dp)
            SelectionRow("Required capabilities", listOf("tools", "reasoning", "structured_output", "editing"), ctl.explainCapabilities) { ctl.explainCapabilities = toggleItem(ctl.explainCapabilities, it) }
            Gap(10.dp)
            SelectionRow("Thinking level", listOf("any", "none", "low", "medium", "high"), listOf(ctl.explainThinkingLevel ?: "any")) { ctl.explainThinkingLevel = it.takeUnless { v -> v == "any" } }
            Gap(10.dp)
            TriStateControl("Code / content editing", ctl.explainEditable) { ctl.explainEditable = it }
            Gap(10.dp)
            TriStateControl("Tool call capability", ctl.explainToolCalls) { ctl.explainToolCalls = it }
            Gap(10.dp)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                RouterField(ctl.explainMinContext, { ctl.explainMinContext = it }, Modifier.widthIn(max = 220.dp), label = "Minimum context tokens", placeholder = "any", numeric = true)
                RouterField(ctl.explainMaxPrice, { ctl.explainMaxPrice = it }, Modifier.widthIn(max = 220.dp), label = "Price ceiling · USD / M tokens", placeholder = "router policy", numeric = true, decimal = true)
            }
        }
        Gap(14.dp)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            RouterButton(if (ctl.explainLoading) "Explaining…" else "Explain route", { ctl.runExplain() }, primary = true, enabled = validation == null && !ctl.explainLoading)
            if (ctl.explainLoading) T("Reading router evidence…", ty.small, c.fgMuted)
            else if (validation != null && ctl.explainPrompt.isNotBlank()) T(validation, ty.small, c.amberText)
        }
    }
}

@Composable
private fun ExplainOutcome(ctl: AppController) {
    val c = Ink.colors
    val ty = Ink.type
    ctl.explainError?.let { T("Could not explain: $it", ty.bodyStrong, c.amberText) }
    ctl.explainResult?.let { ChoiceCard(it.choice, title = "Last router choice") }
    if (ctl.explainResult == null && ctl.explainError == null) {
        T("Router choice", ty.title, c.fg)
        Gap(8.dp)
        T(if (ctl.explainLoading) "Reading the router's evidence…" else "Enter context and choose requirements, then explain the route.", ty.body, c.fgMuted)
    }
}

private fun toggleItem(items: List<String>, item: String): List<String> = if (item in items) items - item else items + item

@Composable
private fun SelectionRow(label: String, options: List<String>, selected: List<String>, onSelect: (String) -> Unit) {
    T(label, Ink.type.smallStrong, Ink.colors.fgMuted)
    Gap(4.dp)
    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        options.forEach { option -> RouterButton(option.replace('_', ' '), { onSelect(option) }, selected = option in selected) }
    }
}

@Composable
private fun TriStateControl(label: String, value: Boolean?, onChange: (Boolean?) -> Unit) {
    val selected = when (value) { true -> "required"; false -> "excluded"; null -> "any" }
    SelectionRow(label, listOf("any", "required", "excluded"), listOf(selected)) {
        onChange(when (it) { "required" -> true; "excluded" -> false; else -> null })
    }
}

@Composable
private fun SeatCapabilities(seat: Candidate) {
    val c = Ink.colors
    val ty = Ink.type
    fun state(value: Boolean?) = when (value) { true -> "yes"; false -> "no"; null -> "UNKNOWN" }
    Column(Modifier.fillMaxWidth().background(c.surfaceSunk).padding(10.dp)) {
        FlowRow(horizontalArrangement = Arrangement.spacedBy(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            LabeledText("context tokens", seat.contextLength?.toString() ?: "UNKNOWN")
            LabeledText("available", state(seat.available))
            LabeledText("tools", state(seat.supportsTools))
            LabeledText("reasoning", state(seat.supportsReasoning))
            LabeledText("editing", state(seat.supportsEditing))
            LabeledText("thinking levels", seat.reasoningEfforts?.takeIf { it.isNotEmpty() }?.joinToString(", ") ?: "UNKNOWN")
            LabeledText("measured USD / M", Fmt.perMtok(seat.measuredUsdPerMtok).text)
            LabeledText("list input / output USD / M", "${Fmt.perMtok(seat.listPrompt).text} / ${Fmt.perMtok(seat.listCompletion).text}")
            LabeledText("price source", seat.priceSource.ifBlank { "UNKNOWN" })
        }
        Gap(6.dp)
        T("Modalities: ${seat.inputModalities?.joinToString(", ") ?: "UNKNOWN"} → ${seat.outputModalities?.joinToString(", ") ?: "UNKNOWN"}", ty.small, c.fgMuted)
        T("Supported parameters: ${seat.supportedParameters?.joinToString(", ") ?: "UNKNOWN"}", ty.small, c.fgMuted)
    }
}

// ---- Setup ----------------------------------------------------------------------------------

@Composable
fun SetupScreen(ctl: AppController) {
    LaunchedEffect(ctl.settings) { if (ctl.setup == null && ctl.setupError == null) ctl.loadSetup() }
    val c = Ink.colors
    val ty = Ink.type
    var url by remember(ctl.settings) { mutableStateOf(ctl.settings.baseUrl) }
    var token by remember(ctl.settings) { mutableStateOf(ctl.settings.token) }
    var testResult by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 700.dp)) {
        T("Setup", ty.title, c.fg)
        Gap(12.dp)
        RouterField(url, { url = it }, label = "Router URL")
        Gap(10.dp)
        RouterField(token, { token = it }, label = "Router token (blank if no_auth)", secret = true)
        Gap(6.dp)
        T(ctl.storageNote, ty.small, c.fgMuted)
        Gap(6.dp)
        T("Never logged or displayed once saved.", ty.small, c.fgFaint)
        Gap(14.dp)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            RouterButton("Save", {
                ctl.saveSettings(RouterSettings(url.trim(), token))
                testResult = "Saved."
            }, primary = true)
            RouterButton("Test connection", {
                testResult = "Testing"
                scope.launch { testResult = ctl.testConnection(RouterSettings(url.trim(), token)) }
            })
            RouterButton("Reload", { ctl.loadSetup() })
        }
        testResult?.let { Gap(10.dp); T(it, ty.body, if (it.startsWith("Failed")) c.amberText else c.fg) }
        Gap(20.dp)
        HRule()
        Gap(16.dp)
        if (ctl.setupLoading && ctl.setup == null) T("Loading setup", ty.body, c.fgMuted)
        ctl.setupError?.let { err ->
            T("Router unreachable: $err", ty.bodyStrong, c.amberText)
            Gap(6.dp)
            T("Nothing else in this app will load until the router answers at the URL above.", ty.small, c.fgMuted)
        }
        ctl.setup?.let { s ->
            Row(verticalAlignment = Alignment.CenterVertically) {
                T("Router status", ty.heading, c.fg, Modifier.weight(1f))
                StatusChip(if (s.ready) "READY" else "NOT READY", if (s.ready) ChipKind.Positive else ChipKind.Warning)
            }
            Gap(10.dp)
            LabeledText("config", s.config.ifBlank { "-" })
            Gap(8.dp)
            LabeledText("secrets source", s.secretsSource.ifBlank { "-" } + (s.gcpProject?.let { " ($it)" } ?: ""))
            Gap(8.dp)
            LabeledText("auth", s.auth.ifBlank { "-" })
            Gap(8.dp)
            LabeledText("dashboard", s.dashboard.ifBlank { "-" })
            if (s.configProblems.isNotEmpty()) {
                Gap(10.dp)
                T("Config problems", ty.smallStrong, c.amberText)
                s.configProblems.forEach { T("- $it", ty.small, c.amberText) }
            }
            Gap(16.dp)
            T("Keys", ty.heading, c.fg)
            Gap(6.dp)
            KeysTable(s.keys)
            if (s.dashboardStatus.isNotEmpty()) {
                Gap(16.dp)
                T("Dashboard status", ty.heading, c.fg)
                Gap(6.dp)
                s.dashboardStatus.forEach { (provider, status) ->
                    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
                        T(provider, ty.body, c.fg, Modifier.weight(1f))
                        T(Fmt.jsonValue(status), ty.small, c.fgMuted)
                    }
                }
            }
        }
    }
}

@Composable
private fun KeysTable(keys: List<modelrouter.app.KeyInfo>) {
    val c = Ink.colors
    val ty = Ink.type
    RouterTable {
        Row(Modifier.fillMaxWidth().background(c.surfaceSunk).padding(horizontal = 10.dp, vertical = 6.dp)) {
            T("provider", ty.smallStrong, c.fgMuted, Modifier.weight(1f))
            T("secret name", ty.smallStrong, c.fgMuted, Modifier.weight(1.4f))
            T("source", ty.smallStrong, c.fgMuted, Modifier.weight(0.8f))
            T("present", ty.smallStrong, c.fgMuted, Modifier.weight(0.7f))
            T("detail", ty.smallStrong, c.fgMuted, Modifier.weight(1.2f))
        }
        keys.forEachIndexed { i, k ->
            HRule()
            Row(Modifier.fillMaxWidth().padding(horizontal = 10.dp, vertical = 7.dp), verticalAlignment = Alignment.CenterVertically) {
                T(k.provider, ty.bodyStrong, c.fg, Modifier.weight(1f))
                T(k.name, ty.body, c.fgMuted, Modifier.weight(1.4f), maxLines = 1)
                T(k.source, ty.small, c.fgMuted, Modifier.weight(0.8f))
                Box(Modifier.weight(0.7f)) { StatusChip(if (k.present) "present" else "MISSING", if (k.present) ChipKind.Positive else ChipKind.Warning) }
                T(k.detail.ifBlank { "-" }, ty.small, c.fgMuted, Modifier.weight(1.2f), maxLines = 1)
            }
        }
    }
}
