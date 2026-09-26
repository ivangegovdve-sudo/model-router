package modelrouter.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
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
private fun ProviderTable(providers: List<ProviderState>, dashboard: Map<String, String>) {
    val c = Ink.colors
    val ty = Ink.type
    Column(Modifier.fillMaxWidth().border(1.dp, c.rule)) {
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
                T(dashboard[p.provider] ?: "not carried", ty.small, c.fgMuted, Modifier.weight(1.4f), maxLines = 2)
            }
        }
    }
}

@Composable
private fun SeatsTable(seats: List<Candidate>) {
    val c = Ink.colors
    val ty = Ink.type
    Column(Modifier.fillMaxWidth().border(1.dp, c.rule)) {
        Row(Modifier.fillMaxWidth().background(c.surfaceSunk).padding(horizontal = 10.dp, vertical = 6.dp)) {
            T("seat", ty.smallStrong, c.fgMuted, Modifier.weight(1.8f))
            T("emits", ty.smallStrong, c.fgMuted, Modifier.weight(1.1f))
            T("min tokens", ty.smallStrong, c.fgMuted, Modifier.weight(0.8f))
            T("measured $/M", ty.smallStrong, c.fgMuted, Modifier.weight(0.9f))
            T("list $/M", ty.smallStrong, c.fgMuted, Modifier.weight(0.9f))
            T("latency", ty.smallStrong, c.fgMuted, Modifier.weight(0.7f))
        }
        seats.forEachIndexed { i, s ->
            HRule()
            val unavailable = !s.available || !s.providerState.equals("OK", true)
            Row(
                Modifier.fillMaxWidth()
                    .then(if (unavailable) Modifier.hatched(c.amberHatch, c.amber.copy(alpha = 0.28f)) else Modifier)
                    .padding(horizontal = 10.dp, vertical = 7.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(Modifier.weight(1.8f)) {
                    T(s.seat, ty.body, c.fg, maxLines = 1)
                    if (unavailable) T(s.providerState + (s.providerDetail.takeIf { it.isNotBlank() }?.let { ": $it" } ?: ""), ty.small, c.amberText, maxLines = 1)
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
    val c = Ink.colors
    val ty = Ink.type
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp).widthIn(max = 900.dp)) {
        T("Explain", ty.title, c.fg)
        Gap(4.dp)
        T("Judges a request the same way the router would, with no model call and no cost.", ty.small, c.fgMuted)
        Gap(14.dp)
        RouterField(ctl.explainPrompt, { ctl.explainPrompt = it }, label = "Prompt", placeholder = "What would you ask the router?", singleLine = false, minLines = 4)
        Gap(10.dp)
        Row(verticalAlignment = Alignment.CenterVertically) {
            RouterField(ctl.explainMaxTokens, { v -> ctl.explainMaxTokens = v.filter { it.isDigit() } }, Modifier.widthIn(max = 180.dp), label = "max_tokens (optional)", placeholder = "unset", numeric = true)
            HGap(14.dp)
            RouterToggle(if (ctl.explainNeedsTools) "uses tools: yes" else "uses tools: no", ctl.explainNeedsTools) { ctl.explainNeedsTools = it }
        }
        Gap(14.dp)
        Row(verticalAlignment = Alignment.CenterVertically) {
            RouterButton("Explain (costs nothing)", { ctl.runExplain(ctl.explainPrompt, ctl.explainMaxTokens.toIntOrNull(), ctl.explainNeedsTools) }, primary = true, enabled = ctl.explainPrompt.isNotBlank() && !ctl.explainLoading)
            HGap(10.dp)
            if (ctl.explainLoading) T("Judging, no model call is made", ty.small, c.fgMuted)
        }
        Gap(16.dp)
        ctl.explainError?.let { T("Could not explain: $it", ty.bodyStrong, c.amberText) }
        ctl.explainResult?.let { res ->
            HRule()
            Gap(14.dp)
            ChoiceCard(res.choice, title = "Choice")
        }
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
                        T(status, ty.small, c.fgMuted)
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
    Column(Modifier.fillMaxWidth().border(1.dp, c.rule)) {
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
