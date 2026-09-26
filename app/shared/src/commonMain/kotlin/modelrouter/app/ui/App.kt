package modelrouter.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.ui.graphics.Color
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
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
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import modelrouter.app.Ask
import modelrouter.app.Attempt
import modelrouter.app.Assessment
import modelrouter.app.Choice
import modelrouter.app.Decision
import modelrouter.app.DecisionRow
import modelrouter.app.Fmt
import modelrouter.app.RouterApi
import modelrouter.app.RouterSettings
import modelrouter.app.SettingsStore

enum class Screen { Decisions, Roster, Explain, Setup }

/** Screen state and every server call. Holds no token beyond [settings]. */
class AppController(private val store: SettingsStore?, private val scope: CoroutineScope) {
    var settings by mutableStateOf(store?.load() ?: RouterSettings())
    var screen by mutableStateOf(Screen.Decisions)
    val storageNote: String get() = store?.storageNote ?: "No settings store"

    var decisions by mutableStateOf<List<DecisionRow>>(emptyList())
    var decisionsError by mutableStateOf<String?>(null)
    var decisionsLoading by mutableStateOf(false)

    var selectedId by mutableStateOf<String?>(null)
    var selectedDecision by mutableStateOf<Decision?>(null)
    var decisionError by mutableStateOf<String?>(null)
    var decisionLoading by mutableStateOf(false)

    var roster by mutableStateOf<modelrouter.app.RosterResponse?>(null)
    var rosterError by mutableStateOf<String?>(null)
    var rosterLoading by mutableStateOf(false)
    var measuredOnly by mutableStateOf(true)

    var setup by mutableStateOf<modelrouter.app.Setup?>(null)
    var setupError by mutableStateOf<String?>(null)
    var setupLoading by mutableStateOf(false)

    var explainResult by mutableStateOf<modelrouter.app.ExplainResponse?>(null)
    var explainError by mutableStateOf<String?>(null)
    var explainLoading by mutableStateOf(false)
    var explainPrompt by mutableStateOf("")
    var explainMaxTokens by mutableStateOf("")
    var explainNeedsTools by mutableStateOf(false)

    private fun api() = RouterApi(settings)
    private fun describe(t: Throwable): String = (t.message ?: t::class.simpleName ?: "error").take(400)

    fun saveSettings(s: RouterSettings) {
        settings = s
        store?.save(s)
        decisions = emptyList(); roster = null; setup = null
    }

    suspend fun testConnection(s: RouterSettings): String {
        val api = RouterApi(s)
        return try {
            val h = api.health()
            "Connected: ${h.status}. Service ${h.service} ${h.version}."
        } catch (t: Throwable) { "Failed: ${describe(t)}" } finally { api.close() }
    }

    fun loadDecisions() = scope.launch {
        decisionsLoading = true; decisionsError = null
        val api = api()
        try { decisions = api.decisions(100) } catch (t: Throwable) { decisionsError = describe(t) }
        finally { api.close(); decisionsLoading = false }
    }

    fun openDecision(id: String) {
        selectedId = id
        selectedDecision = null
        decisionError = null
        decisionLoading = true
        scope.launch {
            val api = api()
            try { selectedDecision = api.decision(id) } catch (t: Throwable) { decisionError = describe(t) }
            finally { api.close(); decisionLoading = false }
        }
    }

    fun closeDecision() { selectedId = null; selectedDecision = null; decisionError = null }

    fun loadRoster() = scope.launch {
        rosterLoading = true; rosterError = null
        val api = api()
        try { roster = api.roster(measuredOnly = measuredOnly) } catch (t: Throwable) { rosterError = describe(t) }
        finally { api.close(); rosterLoading = false }
    }

    fun loadSetup() = scope.launch {
        setupLoading = true; setupError = null
        val api = api()
        try { setup = api.setup() } catch (t: Throwable) { setupError = describe(t) }
        finally { api.close(); setupLoading = false }
    }

    fun runExplain(prompt: String, maxTokens: Int?, needsTools: Boolean) = scope.launch {
        explainLoading = true; explainError = null; explainResult = null
        val api = api()
        try {
            val req = modelrouter.app.ExplainRequest(
                model = "auto",
                messages = listOf(modelrouter.app.ExplainMessage("user", prompt)),
                maxTokens = maxTokens,
                tools = if (needsTools) listOf(kotlinx.serialization.json.JsonObject(mapOf("type" to kotlinx.serialization.json.JsonPrimitive("function")))) else null,
            )
            explainResult = api.explain(req)
        } catch (t: Throwable) { explainError = describe(t) } finally { api.close(); explainLoading = false }
    }
}

@Composable
fun ModelRouterApp(store: SettingsStore?, dark: Boolean, onController: (AppController) -> Unit = {}) {
    RouterTheme(dark = dark) {
        val scope = rememberCoroutineScope()
        val ctl = remember { AppController(store, scope) }
        LaunchedEffect(Unit) { onController(ctl) }
        val c = Ink.colors
        Column(Modifier.fillMaxSize().background(c.bg)) {
            TopBar(ctl)
            HRule(c.ruleStrong)
            Box(Modifier.fillMaxSize()) {
                when (ctl.screen) {
                    Screen.Decisions -> DecisionsScreen(ctl)
                    Screen.Roster -> RosterScreen(ctl)
                    Screen.Explain -> ExplainScreen(ctl)
                    Screen.Setup -> SetupScreen(ctl)
                }
            }
        }
    }
}

@Composable
private fun TopBar(ctl: AppController) {
    val c = Ink.colors
    Row(
        Modifier.fillMaxWidth().background(c.surface).padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        T("modelrouter", Ink.type.heading, c.fg, maxLines = 1)
        HGap(18.dp)
        Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            RouterButton("Decisions", { ctl.screen = Screen.Decisions; if (ctl.decisions.isEmpty()) ctl.loadDecisions() }, selected = ctl.screen == Screen.Decisions)
            RouterButton("Roster", { ctl.screen = Screen.Roster; if (ctl.roster == null) ctl.loadRoster() }, selected = ctl.screen == Screen.Roster)
            RouterButton("Explain", { ctl.screen = Screen.Explain }, selected = ctl.screen == Screen.Explain)
            RouterButton("Setup", { ctl.screen = Screen.Setup; if (ctl.setup == null) ctl.loadSetup() }, selected = ctl.screen == Screen.Setup)
        }
    }
}

// ---- Decisions -----------------------------------------------------------------------------

@Composable
private fun DecisionsScreen(ctl: AppController) {
    LaunchedEffect(ctl.settings) { if (ctl.decisions.isEmpty() && ctl.decisionsError == null) ctl.loadDecisions() }
    BoxWithConstraints(Modifier.fillMaxSize()) {
        val wide = maxWidth >= 900.dp
        if (wide) {
            Row(Modifier.fillMaxSize()) {
                DecisionsList(ctl, Modifier.width(420.dp).fillMaxHeight())
                VRule()
                Box(Modifier.weight(1f).fillMaxHeight()) { DecisionDetailPane(ctl, showBack = false) }
            }
        } else {
            if (ctl.selectedId == null) DecisionsList(ctl, Modifier.fillMaxSize())
            else DecisionDetailPane(ctl, showBack = true)
        }
    }
}

@Composable
private fun DecisionsList(ctl: AppController, modifier: Modifier) {
    val c = Ink.colors
    val ty = Ink.type
    Column(modifier) {
        Row(Modifier.fillMaxWidth().padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            T("Recent decisions", ty.heading, c.fg, Modifier.weight(1f))
            RouterButton("Refresh", { ctl.loadDecisions() })
        }
        HRule()
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
            if (ctl.decisionsLoading && ctl.decisions.isEmpty()) T("Loading decisions", ty.body, c.fgMuted, Modifier.padding(12.dp))
            ctl.decisionsError?.let { err ->
                Column(Modifier.padding(12.dp)) {
                    T("Router unreachable: $err", ty.bodyStrong, c.amberText)
                    Gap(6.dp)
                    T("Check the base URL and token in Setup.", ty.small, c.fgMuted)
                    Gap(8.dp)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        RouterButton("Retry", { ctl.loadDecisions() })
                        RouterButton("Setup", { ctl.screen = Screen.Setup })
                    }
                }
            }
            if (!ctl.decisionsLoading && ctl.decisions.isEmpty() && ctl.decisionsError == null) {
                T("No decisions recorded yet.", ty.body, c.fgMuted, Modifier.padding(12.dp))
            }
            ctl.decisions.forEach { row -> DecisionRowView(row, selected = row.id == ctl.selectedId) { ctl.openDecision(row.id) }; HRule() }
        }
    }
}

@Composable
private fun DecisionRowView(row: DecisionRow, selected: Boolean, onClick: () -> Unit) {
    val c = Ink.colors
    val ty = Ink.type
    val refusal = row.isRefusal
    val src = remember { MutableInteractionSource() }
    val focused by src.collectIsFocusedAsState()
    Column(
        Modifier.fillMaxWidth()
            .background(if (selected) c.surfaceSunk else c.bg)
            .then(if (refusal) Modifier.hatched(c.amberHatch, c.amber.copy(alpha = 0.35f)) else Modifier)
            // Keyboard focus must be visible (WCAG 2.4.7).
            .border(if (focused) 2.dp else 0.dp, if (focused) c.fg else Color.Transparent)
            .clickable(interactionSource = src, indication = null, role = Role.Button,
                       onClickLabel = "Open decision", onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 9.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            N(clockText(row.t), c.fgFaint, Modifier.width(64.dp))
            HGap(8.dp)
            T(row.requested, ty.bodyStrong, c.fg, Modifier.weight(1f), maxLines = 1)
            HGap(8.dp)
            StatusChip(row.status.ifBlank { row.outcome }, if (refusal) ChipKind.Warning else ChipKind.Positive)
        }
        Gap(4.dp)
        Row(verticalAlignment = Alignment.CenterVertically) {
            T(row.seat ?: "no seat", ty.small, c.fgMuted, Modifier.weight(1f), maxLines = 1)
            val cost = Fmt.cost(row.costUsd, row.costBasis)
            // An abstention made no call: its cost is known to be nothing, not unknown.
            if (row.costUsd == null && row.status.equals("ABSTAINED", true)) T("no call", ty.small, c.fgMuted)
            else if (cost.unknown) T("UNKNOWN", ty.smallStrong, c.amberText) else Row {
                N(cost.amount, c.fg, strong = true); HGap(4.dp); T(cost.basis, ty.small, c.fgMuted)
            }
        }
        Gap(3.dp)
        T(row.because, ty.small, c.fgMuted, maxLines = 2)
    }
}

@Composable
private fun DecisionDetailPane(ctl: AppController, showBack: Boolean) {
    val c = Ink.colors
    val ty = Ink.type
    val id = ctl.selectedId
    Column(Modifier.fillMaxSize()) {
        if (showBack && id != null) {
            Row(Modifier.fillMaxWidth().padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                RouterButton("Back", { ctl.closeDecision() })
                HGap(10.dp)
                T(id, ty.smallStrong, c.fgMuted, maxLines = 1)
            }
            HRule()
        }
        if (id == null) {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { T("Select a decision to see why.", ty.body, c.fgMuted) }
            return
        }
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
            if (ctl.decisionLoading) T("Loading decision $id", ty.body, c.fgMuted)
            ctl.decisionError?.let { err ->
                Column {
                    T("Could not load decision: $err", ty.bodyStrong, c.amberText)
                    Gap(8.dp)
                    RouterButton("Retry", { ctl.openDecision(id) })
                }
            }
            ctl.selectedDecision?.let { d -> DecisionDetail(d) }
        }
    }
}

@Composable
private fun DecisionDetail(d: Decision) {
    val c = Ink.colors
    val ty = Ink.type
    if (!d.id.isBlank()) { T(d.id, ty.smallStrong, c.fgFaint, maxLines = 1); Gap(2.dp) }
    Row(verticalAlignment = Alignment.CenterVertically) {
        T(d.requested, ty.title, c.fg, Modifier.weight(1f), maxLines = 1)
        StatusChip(d.status, if (Fmt.statusIsRefusal(d.status)) ChipKind.Warning else ChipKind.Positive)
    }
    Gap(6.dp)
    // The question this screen exists to answer -- what did it cost, and on what basis --
    // goes at the top, not at the bottom of the attempts table.
    Row(verticalAlignment = Alignment.CenterVertically) {
        val cost = Fmt.cost(d.result.costUsd, d.result.costBasis)
        T("cost ", ty.small, c.fgMuted)
        when {
            d.attempts.isEmpty() -> T("nothing -- no model was called", ty.bodyStrong, c.fg)
            cost.unknown -> T("UNKNOWN", ty.smallStrong, c.amberText)
            else -> { N(cost.amount, c.fg, strong = true); HGap(4.dp); T(cost.basis, ty.small, c.fgMuted) }
        }
        d.choice.expectedUsd?.let { HGap(14.dp); T("expected ", ty.small, c.fgMuted); N(Fmt.money(it), c.fgMuted) }
        HGap(14.dp)
        T("${d.attempts.size} call" + (if (d.attempts.size == 1) "" else "s"), ty.small, c.fgMuted)
    }
    Gap(10.dp)
    AskCard(d.ask)
    Gap(14.dp)
    HRule()
    Gap(14.dp)
    ChoiceCard(d.choice, title = "Decision")
    if (d.earlierDecisions.isNotEmpty()) {
        Gap(16.dp)
        T("Earlier decisions in this call (retried after a failure)", ty.heading, c.fg)
        Gap(6.dp)
        d.earlierDecisions.forEachIndexed { i, ch -> Gap(if (i == 0) 0.dp else 10.dp); ChoiceCard(ch, title = null) }
    }
    Gap(16.dp)
    HRule()
    Gap(14.dp)
    T("Attempts", ty.heading, c.fg)
    Gap(6.dp)
    if (d.attempts.isEmpty()) T("No attempts were made.", ty.small, c.fgMuted) else AttemptsTable(d.attempts)
    Gap(16.dp)
    HRule()
    Gap(14.dp)
    ResultCard(d)
}

@Composable
private fun AskCard(ask: Ask) {
    val c = Ink.colors
    val ty = Ink.type
    T("The ask", ty.heading, c.fg)
    Gap(6.dp)
    Row(horizontalArrangement = Arrangement.spacedBy(20.dp)) {
        LabeledNum("prompt tokens", Fmt.tokens(ask.promptTokens))
        LabeledNum("max tokens", ask.maxTokens?.toString() ?: "unset")
        LabeledNum("ceiling $/M", ask.ceilingUsdPerMtok?.let { Fmt.perMtok(it).text } ?: "none")
        LabeledText("tools", if (ask.needsTools) "required" else "no")
        LabeledText("stream", if (ask.stream) "yes" else "no")
    }
}

@Composable
private fun LabeledNum(label: String, value: String) {
    Column {
        T(label, Ink.type.small, Ink.colors.fgMuted)
        N(value, Ink.colors.fg, strong = true)
    }
}

@Composable
internal fun LabeledText(label: String, value: String) {
    Column {
        T(label, Ink.type.small, Ink.colors.fgMuted)
        T(value, Ink.type.bodyStrong, Ink.colors.fg)
    }
}

@Composable
fun ChoiceCard(choice: Choice, title: String?) {
    val c = Ink.colors
    val ty = Ink.type
    Column(Modifier.fillMaxWidth()) {
        title?.let { T(it, ty.heading, c.fg); Gap(6.dp) }
        Row(verticalAlignment = Alignment.CenterVertically) {
            StatusChip(choice.outcome, if (choice.isRoute) ChipKind.Positive else ChipKind.Warning)
            HGap(10.dp)
            T(choice.seat ?: "no seat chosen", ty.bodyStrong, c.fg, Modifier.weight(1f), maxLines = 1)
            choice.expectedUsd?.let { N("expected " + Fmt.cost(it, "computed").amount, c.fgMuted) }
        }
        Gap(6.dp)
        val bg = if (choice.isRoute) Modifier else Modifier.hatched(c.amberHatch, c.amber.copy(alpha = 0.32f))
        Column(bg.padding(if (choice.isRoute) 0.dp else 8.dp)) {
            T(choice.because, ty.body, c.fg)
        }
        Gap(10.dp)
        FactsRow(choice)
        Gap(10.dp)
        if (choice.considered.isNotEmpty()) ConsideredTable(choice)
    }
}

@Composable
private fun FactsRow(choice: Choice) {
    val c = Ink.colors
    val f = choice.facts
    Row(horizontalArrangement = Arrangement.spacedBy(18.dp)) {
        LabeledNum("roster size", f.rosterSize?.toString() ?: "?")
        LabeledNum("unknown, not listed", f.unknownNotListed?.toString() ?: "0")
        LabeledNum("free tier, not listed", f.freeTierNotListed?.toString() ?: "0")
        if (choice.unknown.isNotEmpty()) LabeledText("missing facts", choice.unknown.joinToString(", "))
    }
}

/** QUALIFIES ranked first with the winner marked, then EXCLUDED, then UNKNOWN. Order is the router's; never re-sorted here. */
@Composable
fun ConsideredTable(choice: Choice) {
    val c = Ink.colors
    val ty = Ink.type
    val winnerSeat = choice.seat
    Column(Modifier.fillMaxWidth().border(1.dp, c.rule)) {
        ConsideredHeaderRow()
        HRule()
        choice.considered.forEachIndexed { i, a ->
            ConsideredRow(a, isWinner = a.seat == winnerSeat && a.verdict.equals("QUALIFIES", true))
            if (i != choice.considered.lastIndex) HRule()
        }
    }
}

@Composable
private fun ConsideredHeaderRow() {
    val c = Ink.colors
    Row(Modifier.fillMaxWidth().background(c.surfaceSunk).padding(horizontal = 10.dp, vertical = 6.dp)) {
        T("seat", Ink.type.smallStrong, c.fgMuted, Modifier.weight(1.6f))
        T("verdict", Ink.type.smallStrong, c.fgMuted, Modifier.weight(0.9f))
        T("$/M", Ink.type.smallStrong, c.fgMuted, Modifier.weight(0.7f))
        T("expected", Ink.type.smallStrong, c.fgMuted, Modifier.weight(0.8f))
        T("because", Ink.type.smallStrong, c.fgMuted, Modifier.weight(2.2f))
    }
}

@Composable
private fun ConsideredRow(a: Assessment, isWinner: Boolean) {
    val c = Ink.colors
    val ty = Ink.type
    val kind = when {
        a.verdict.equals("QUALIFIES", true) -> ChipKind.Positive
        a.verdict.equals("EXCLUDED", true) -> ChipKind.Excluded
        else -> ChipKind.Warning
    }
    Row(
        Modifier.fillMaxWidth().background(if (isWinner) c.surfaceSunk else c.bg).padding(horizontal = 10.dp, vertical = 7.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Row(Modifier.weight(1.6f)) {
            if (isWinner) T("✦ ", ty.bodyStrong, c.accent)
            T(a.seat, if (isWinner) ty.bodyStrong else ty.body, c.fg, maxLines = 2)
        }
        Box(Modifier.weight(0.9f)) { StatusChip(a.verdict, kind) }
        val price = Fmt.perMtok(a.usdPerMtok)
        Box(Modifier.weight(0.7f)) {
            // Excluded before pricing: the price was never consulted, which is not "unknown".
            if (price.unknown && a.verdict == "EXCLUDED") T("-", ty.small, c.fgFaint)
            else if (price.unknown) T("UNKNOWN", ty.smallStrong, c.amberText) else N(price.text, c.fg, strong = isWinner)
        }
        Box(Modifier.weight(0.8f)) {
            val exp = a.expectedUsd
            if (exp == null) T("-", ty.small, c.fgFaint) else N(Fmt.cost(exp, "computed").amount, c.fgMuted)
        }
        T(a.because, ty.small, c.fgMuted, Modifier.weight(2.2f), maxLines = 3)
    }
}

@Composable
fun AttemptsTable(attempts: List<Attempt>) {
    val c = Ink.colors
    val ty = Ink.type
    Column(Modifier.fillMaxWidth().border(1.dp, c.rule)) {
        Row(Modifier.fillMaxWidth().background(c.surfaceSunk).padding(horizontal = 10.dp, vertical = 6.dp)) {
            T("seat", ty.smallStrong, c.fgMuted, Modifier.weight(1.4f))
            T("http", ty.smallStrong, c.fgMuted, Modifier.weight(0.6f))
            T("result", ty.smallStrong, c.fgMuted, Modifier.weight(0.8f))
            T("content", ty.smallStrong, c.fgMuted, Modifier.weight(0.8f))
            T("reasoning", ty.smallStrong, c.fgMuted, Modifier.weight(0.8f))
            T("cost", ty.smallStrong, c.fgMuted, Modifier.weight(1.0f))
            T("latency", ty.smallStrong, c.fgMuted, Modifier.weight(0.8f))
        }
        attempts.forEachIndexed { i, a ->
            HRule()
            val label = Fmt.attemptLabel(a)
            val isEmpty = a.isEmpty
            Row(
                Modifier.fillMaxWidth()
                    .then(if (isEmpty) Modifier.hatched(c.amberHatch, c.amber.copy(alpha = 0.32f)) else Modifier)
                    .padding(horizontal = 10.dp, vertical = 7.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                T(a.seat, ty.body, c.fg, Modifier.weight(1.4f), maxLines = 2)
                N(a.http?.toString() ?: "-", c.fgMuted, Modifier.weight(0.6f))
                Box(Modifier.weight(0.8f)) {
                    StatusChip(label, if (isEmpty) ChipKind.Warning else if (a.ok) ChipKind.Positive else ChipKind.Excluded)
                }
                N(a.contentChars.toString(), if (isEmpty) c.amberText else c.fg, Modifier.weight(0.8f), strong = isEmpty)
                N(a.reasoningChars.toString(), c.fgMuted, Modifier.weight(0.8f))
                val cost = Fmt.cost(a.costUsd, a.costBasis)
                Box(Modifier.weight(1.0f)) {
                    if (cost.unknown) T("UNKNOWN", ty.smallStrong, c.amberText) else Row {
                        N(cost.amount, c.fg); HGap(4.dp); T(cost.basis, ty.small, c.fgMuted)
                    }
                }
                N(Fmt.seconds(a.latencyS), c.fgMuted, Modifier.weight(0.8f))
            }
        }
    }
}

@Composable
private fun ResultCard(d: Decision) {
    val c = Ink.colors
    val ty = Ink.type
    T("Result", ty.heading, c.fg)
    Gap(6.dp)
    Row(horizontalArrangement = Arrangement.spacedBy(20.dp), verticalAlignment = Alignment.CenterVertically) {
        val cost = Fmt.cost(d.result.costUsd, d.result.costBasis)
        Column {
            T("cost", ty.small, c.fgMuted)
            if (cost.unknown) T("UNKNOWN", ty.bodyStrong, c.amberText) else Row {
                N(cost.amount, c.fg, strong = true, large = true); HGap(6.dp); T(cost.basis, ty.small, c.fgMuted)
            }
        }
        LabeledNum("calls", d.result.calls.toString())
        if (d.result.callsUnknownCost > 0) LabeledNum("calls, UNKNOWN cost", d.result.callsUnknownCost.toString())
        d.elapsedS?.let { LabeledNum("elapsed", Fmt.seconds(it)) }
    }
}

/** Unix-seconds to a compact clock string, no locale/timezone library needed for an instrument label. */
internal fun clockText(unixSeconds: Double): String {
    val totalSeconds = unixSeconds.toLong()
    val secOfDay = ((totalSeconds % 86400) + 86400) % 86400
    val h = secOfDay / 3600
    val m = (secOfDay % 3600) / 60
    val s = secOfDay % 60
    fun p(v: Long) = v.toString().padStart(2, '0')
    return "${p(h)}:${p(m)}:${p(s)}"
}
