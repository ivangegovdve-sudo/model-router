package modelrouter.app.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import modelrouter.app.res.Res
import modelrouter.app.res.plex_mono_medium
import modelrouter.app.res.plex_mono_regular
import modelrouter.app.res.plex_sans
import org.jetbrains.compose.resources.Font

/**
 * Every colour, shape and type decision lives here. This is an instrument, not a brochure:
 * density is high, motion is near zero, and colour is rationed. One accent (steel blue) marks
 * a live routing decision (ROUTE / QUALIFIES). Amber marks the thing this product exists to
 * surface: UNKNOWN prices, refusals, and empty responses. Nothing else is saturated.
 */
@Immutable
data class RouterColors(
    val bg: Color,
    val surface: Color,
    val surfaceSunk: Color,
    val rule: Color,
    val ruleStrong: Color,
    val fg: Color,
    val fgMuted: Color,
    val fgFaint: Color,
    val accent: Color,
    val onAccent: Color,
    val amber: Color,
    val amberText: Color,
    val amberHatch: Color,
    val excluded: Color,
    val dark: Boolean,
)

val DarkColors = RouterColors(
    bg = Color(0xFF111214),
    surface = Color(0xFF17181B),
    surfaceSunk = Color(0xFF0D0E10),
    rule = Color(0xFF2A2C30),
    ruleStrong = Color(0xFF3B3E44),
    fg = Color(0xFFE7E7E4),
    fgMuted = Color(0xFF9DA0A5),
    fgFaint = Color(0xFF84878C), // >= 4.5:1 on bg/surface
    accent = Color(0xFF5FA8D3),  // steel blue: a decision was made
    onAccent = Color(0xFF0B1116),
    amber = Color(0xFFC98A1B),
    amberText = Color(0xFFE0A33A),
    amberHatch = Color(0x33C98A1B),
    excluded = Color(0xFF8C9096), // >= 4.5:1 on bg/surface (audit 2026-09-26)
    dark = true,
)

val LightColors = RouterColors(
    bg = Color(0xFFF2F2F0),
    surface = Color(0xFFFBFBFA),
    surfaceSunk = Color(0xFFE8E8E5),
    rule = Color(0xFFD4D4D0),
    ruleStrong = Color(0xFFB6B6B1),
    fg = Color(0xFF17181A),
    fgMuted = Color(0xFF54575B),
    fgFaint = Color(0xFF64676B), // >= 4.5:1 on bg/surface
    accent = Color(0xFF1F6E9C),  // >= 4.5:1 on light surfaces
    onAccent = Color(0xFFFFFFFF),
    amber = Color(0xFFC98A1B),
    amberText = Color(0xFF8F5D09),
    amberHatch = Color(0x40C98A1B),
    excluded = Color(0xFF62656A), // >= 4.5:1 on bg/surface (audit 2026-09-26)
    dark = false,
)

@Immutable
data class RouterType(
    val sans: FontFamily,
    val mono: FontFamily,
) {
    val title get() = TextStyle(fontFamily = sans, fontWeight = FontWeight.SemiBold, fontSize = 19.sp, lineHeight = 25.sp)
    val heading get() = TextStyle(fontFamily = sans, fontWeight = FontWeight.SemiBold, fontSize = 14.sp, lineHeight = 19.sp)
    val body get() = TextStyle(fontFamily = sans, fontWeight = FontWeight.Normal, fontSize = 13.sp, lineHeight = 18.sp)
    val bodyStrong get() = body.copy(fontWeight = FontWeight.SemiBold)
    val small get() = TextStyle(fontFamily = sans, fontWeight = FontWeight.Normal, fontSize = 11.5.sp, lineHeight = 15.sp)
    val smallStrong get() = small.copy(fontWeight = FontWeight.SemiBold)
    /** Mono is for numbers only: prices, latency, counts, tokens, costs. Never a section label. */
    val num get() = TextStyle(fontFamily = mono, fontWeight = FontWeight.Normal, fontSize = 12.sp, lineHeight = 16.sp)
    val numStrong get() = TextStyle(fontFamily = mono, fontWeight = FontWeight.Medium, fontSize = 12.5.sp, lineHeight = 17.sp)
    val numLarge get() = TextStyle(fontFamily = mono, fontWeight = FontWeight.Medium, fontSize = 16.sp, lineHeight = 21.sp)
}

object Shapes {
    val corner = RoundedCornerShape(2.dp)
}

val LocalColors = staticCompositionLocalOf { DarkColors }
val LocalType = staticCompositionLocalOf<RouterType> { error("RouterTheme not set") }

object Ink {
    val colors: RouterColors @Composable get() = LocalColors.current
    val type: RouterType @Composable get() = LocalType.current
}

@Composable
fun RouterTheme(dark: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    val sans = FontFamily(
        Font(Res.font.plex_sans, FontWeight.Normal),
        Font(Res.font.plex_sans, FontWeight.Medium),
        Font(Res.font.plex_sans, FontWeight.SemiBold),
        Font(Res.font.plex_sans, FontWeight.Bold),
    )
    val mono = FontFamily(
        Font(Res.font.plex_mono_regular, FontWeight.Normal),
        Font(Res.font.plex_mono_medium, FontWeight.Medium),
    )
    CompositionLocalProvider(
        LocalColors provides if (dark) DarkColors else LightColors,
        LocalType provides RouterType(sans, mono),
        content = content,
    )
}
