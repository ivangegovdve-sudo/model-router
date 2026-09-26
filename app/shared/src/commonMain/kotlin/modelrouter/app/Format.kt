package modelrouter.app

import kotlin.math.abs
import kotlin.math.pow
import kotlin.math.roundToLong

/**
 * Number formatting without platform String.format, so common code and tests agree.
 * Every function here follows the contract's hard rendering rules (docs/CONTRACT.md):
 *  - null price is UNKNOWN, never $0, never sorted as cheapest.
 *  - cost_basis is always shown next to a cost.
 *  - ABSTAIN and FAILED are refusals, never a gap.
 *  - an attempt with content_chars == 0 is EMPTY, even when http is 200.
 */
object Fmt {
    fun fixed(v: Double, decimals: Int): String {
        val neg = v < 0
        val factor = 10.0.pow(decimals)
        val scaled = (abs(v) * factor).roundToLong()
        val whole = scaled / factor.toLong()
        val frac = scaled % factor.toLong()
        val fracStr = if (decimals == 0) "" else "." + frac.toString().padStart(decimals, '0')
        return (if (neg) "-" else "") + whole.toString() + fracStr
    }

    /** $/Mtok with enough precision to tell cheap models apart. Null is UNKNOWN, never $0. */
    fun perMtok(v: Double?): PriceText {
        if (v == null) return PriceText("UNKNOWN", unknown = true)
        val text = "$" + when {
            v >= 10 -> fixed(v, 2)
            v >= 1 -> fixed(v, 3)
            else -> fixed(v, 4)
        }
        return PriceText(text, unknown = false)
    }

    /** Per-call cost with its basis always attached, per the contract's hard rule. */
    fun cost(usd: Double?, basis: String?): CostText {
        if (usd == null) return CostText("UNKNOWN", basis?.ifBlank { null } ?: "unknown", unknown = true)
        return CostText(money(usd), basis?.ifBlank { null } ?: "unknown", unknown = false)
    }

    /**
     * Dollars with at least two significant figures. Routed calls cost fractions of a
     * micro-dollar; a fixed six decimals renders $0.00000025 as "$0.000000", which reads
     * as free -- the one thing a cost column must never imply.
     */
    fun money(usd: Double): String {
        if (usd == 0.0) return "$0"
        var decimals = 6
        while (decimals < 12 && abs(usd) * 10.0.pow(decimals) < 10) decimals++
        return "$" + fixed(usd, decimals)
    }

    fun seconds(v: Double?): String = if (v == null) "" else fixed(v, 2) + " s"

    fun ageSeconds(v: Double?): String = if (v == null) "" else fixed(v, 0) + " s ago"

    fun tokens(v: Int?): String = v?.toString() ?: "?"

    /** Attempt outcome label. An HTTP-200 with no content is EMPTY: the failure this app exists to surface. */
    fun attemptLabel(a: Attempt): String = when {
        a.isEmpty -> "EMPTY"
        a.ok -> "OK"
        else -> "FAILED"
    }

    fun outcomeLabel(outcome: String): String = outcome.uppercase()

    fun statusIsRefusal(status: String): Boolean =
        status.equals("ABSTAINED", true) || status.equals("FAILED", true)
}

data class PriceText(val text: String, val unknown: Boolean)
data class CostText(val amount: String, val basis: String, val unknown: Boolean)
