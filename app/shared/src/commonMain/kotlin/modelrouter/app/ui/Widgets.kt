package modelrouter.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.interaction.collectIsHoveredAsState
import androidx.compose.foundation.hoverable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

@Composable
fun T(
    text: String,
    style: TextStyle = Ink.type.body,
    color: Color = Ink.colors.fg,
    modifier: Modifier = Modifier,
    maxLines: Int = Int.MAX_VALUE,
) = BasicText(text, modifier, style.copy(color = color), overflow = TextOverflow.Ellipsis, maxLines = maxLines)

/** Number text: always mono with tabular figures. For money, tokens, latency, counts. */
@Composable
fun N(
    text: String,
    color: Color = Ink.colors.fgMuted,
    modifier: Modifier = Modifier,
    strong: Boolean = false,
    large: Boolean = false,
    maxLines: Int = 1,
) = T(text, when { large -> Ink.type.numLarge; strong -> Ink.type.numStrong; else -> Ink.type.num }, color, modifier, maxLines)

@Composable
fun HRule(color: Color = Ink.colors.rule, thickness: Dp = 1.dp) =
    Box(Modifier.fillMaxWidth().height(thickness).background(color))

@Composable
fun VRule(color: Color = Ink.colors.rule) = Box(Modifier.fillMaxHeight().width(1.dp).background(color))

@Composable
fun Gap(h: Dp) = Spacer(Modifier.height(h))

@Composable
fun HGap(w: Dp) = Spacer(Modifier.width(w))

/** Diagonal amber hatching: the "this is a refusal / this failed" background. Never used for a plain gap. */
fun Modifier.hatched(fill: Color, stroke: Color, spacing: Float = 9f): Modifier = this.clipToBounds().drawBehind {
    drawRect(fill)
    var x = -size.height
    while (x < size.width) {
        drawLine(stroke, Offset(x, size.height), Offset(x + size.height, 0f), strokeWidth = 1.2f)
        x += spacing
    }
}

/** Minimum interactive size: 48dp on Android touch, 32dp with a mouse (desktop). */
val TouchMin: Dp get() = touchMinPlatform

expect val touchMinPlatform: Dp

/** Rectangular button, 2dp corners, 1px rule. Never a pill. */
@Composable
fun RouterButton(
    label: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    primary: Boolean = false,
    enabled: Boolean = true,
    selected: Boolean = false,
) {
    val c = Ink.colors
    val src = remember { MutableInteractionSource() }
    val hovered by src.collectIsHoveredAsState()
    val focused by src.collectIsFocusedAsState()
    val bg = when {
        primary && enabled -> c.fg
        selected -> c.surfaceSunk
        hovered && enabled -> c.surfaceSunk
        else -> c.surface
    }
    val fg = when {
        !enabled -> c.fgFaint
        primary -> c.bg
        else -> c.fg
    }
    Box(
        modifier
            .background(bg, Shapes.corner)
            // Keyboard focus must be visible (WCAG 2.4.7): a 2dp outline, never colour alone.
            .border(if (focused) 2.dp else 1.dp, if (focused || selected || primary) c.fg else c.ruleStrong, Shapes.corner)
            .hoverable(src)
            .clickable(enabled = enabled, interactionSource = src, indication = null, role = Role.Button, onClick = onClick)
            .defaultMinSize(minHeight = TouchMin)
            .padding(horizontal = 12.dp, vertical = 7.dp),
        contentAlignment = Alignment.Center,
    ) { T(label, Ink.type.bodyStrong, fg, maxLines = 1) }
}

@Composable
fun RouterField(
    value: String,
    onValueChange: (String) -> Unit,
    modifier: Modifier = Modifier,
    label: String? = null,
    placeholder: String = "",
    singleLine: Boolean = true,
    minLines: Int = 1,
    numeric: Boolean = false,
    secret: Boolean = false,
) {
    val c = Ink.colors
    Column(modifier) {
        if (label != null) {
            T(label, Ink.type.smallStrong, c.fgMuted)
            Gap(4.dp)
        }
        Box(
            Modifier.fillMaxWidth().background(c.surface, Shapes.corner).border(1.dp, c.ruleStrong, Shapes.corner)
                .padding(horizontal = 10.dp, vertical = 8.dp),
        ) {
            if (value.isEmpty() && placeholder.isNotEmpty()) T(placeholder, Ink.type.body, c.fgFaint)
            BasicTextField(
                value = value,
                onValueChange = onValueChange,
                singleLine = singleLine,
                minLines = minLines,
                textStyle = (if (numeric) Ink.type.num else Ink.type.body).copy(color = c.fg),
                cursorBrush = SolidColor(c.fg),
                keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(
                    keyboardType = if (numeric) KeyboardType.Number else KeyboardType.Text,
                ),
                visualTransformation = if (secret) androidx.compose.ui.text.input.PasswordVisualTransformation()
                else androidx.compose.ui.text.input.VisualTransformation.None,
                modifier = Modifier.fillMaxWidth(),
            )
        }
    }
}

/** A one-line label + toggle, rectangular, never a rounded switch. */
@Composable
fun RouterToggle(label: String, checked: Boolean, modifier: Modifier = Modifier, onCheckedChange: (Boolean) -> Unit) {
    val c = Ink.colors
    Box(
        modifier
            .background(if (checked) c.fg else c.surface, Shapes.corner)
            .border(1.dp, if (checked) c.fg else c.ruleStrong, Shapes.corner)
            .clickable(role = Role.Checkbox) { onCheckedChange(!checked) }
            .defaultMinSize(minHeight = TouchMin)
            .padding(horizontal = 12.dp, vertical = 7.dp),
    ) { T(label, Ink.type.bodyStrong, if (checked) c.bg else c.fg, maxLines = 1) }
}

/** Verdict / outcome chip: square corners, text-forward, colour used sparingly and with intent. */
@Composable
fun StatusChip(text: String, kind: ChipKind, modifier: Modifier = Modifier) {
    val c = Ink.colors
    val (fg, border, bg) = when (kind) {
        ChipKind.Positive -> Triple(c.accent, c.accent, Color.Transparent)
        ChipKind.Warning -> Triple(c.amberText, c.amber, Color.Transparent)
        ChipKind.Neutral -> Triple(c.fgMuted, c.ruleStrong, Color.Transparent)
        ChipKind.Excluded -> Triple(c.excluded, c.rule, Color.Transparent)
    }
    Box(
        modifier.background(bg, Shapes.corner).border(1.dp, border, Shapes.corner)
            .padding(horizontal = 8.dp, vertical = 3.dp),
    ) { T(text, Ink.type.smallStrong, fg, maxLines = 1) }
}

enum class ChipKind { Positive, Warning, Neutral, Excluded }
