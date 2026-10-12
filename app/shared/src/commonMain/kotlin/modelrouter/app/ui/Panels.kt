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

@Composable
fun SeatCapabilities(seat: Candidate) {
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
