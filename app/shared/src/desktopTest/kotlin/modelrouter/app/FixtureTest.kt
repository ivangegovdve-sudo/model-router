package modelrouter.app

import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

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
        assertEquals("ok (676 rows)", d.dashboard["openrouter"])
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
