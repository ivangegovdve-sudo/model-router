package modelrouter.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import modelrouter.app.Choice
import modelrouter.app.Fmt
import kotlinx.serialization.json.JsonNull

/** These are the server's reported facts, never a local prediction of routing success. */
@Composable
fun ChoiceTelemetry(choice: Choice) {
    val c = Ink.colors
    val ty = Ink.type
    Column(Modifier.fillMaxWidth().background(c.surfaceSunk).border(1.dp, c.rule).padding(10.dp)) {
        val confidence = choice.confidence
        Row(Modifier.fillMaxWidth()) {
            T("Routing certainty", ty.heading, c.fg, Modifier.weight(1f))
            StatusChip(confidence?.level?.uppercase() ?: "UNKNOWN", when (confidence?.level) {
                "high" -> ChipKind.Positive
                "medium" -> ChipKind.Neutral
                else -> ChipKind.Warning
            })
        }
        Gap(6.dp)
        if (confidence == null) {
            T("The router did not report certainty for this decision.", ty.small, c.fgMuted)
        } else {
            T(confidence.basis, ty.small, c.fgMuted)
            Gap(6.dp)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(18.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                LabeledText("missing facts", confidence.unknownCount?.toString() ?: "UNKNOWN")
                LabeledText("observations", confidence.sampleCount?.toString() ?: "UNKNOWN")
                LabeledText("calibrated probability", confidence.probability?.let { Fmt.fixed(it * 100, 1) + "%" } ?: "not reported")
            }
            if (confidence.probability == null) {
                Gap(5.dp)
                T("This is evidence-based heuristic certainty, not a probability of a correct answer.", ty.small, c.fgMuted)
            }
            if (confidence.evidence.isNotEmpty()) {
                Gap(6.dp)
                confidence.evidence.forEach { T("• $it", ty.small, c.fgMuted) }
            }
        }
        val features = choice.facts.requestFeatures
        Gap(12.dp)
        T("Context telemetry", ty.heading, c.fg)
        Gap(6.dp)
        choice.facts.lane?.let { LabeledText("routing lane", it); Gap(6.dp) }
        if (features.isEmpty()) T("Request features were not carried by this decision.", ty.small, c.fgMuted)
        else FlowRow(horizontalArrangement = Arrangement.spacedBy(18.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            features.forEach { (name, value) ->
                val text = when {
                    name == "prompt_tokens_basis" && Fmt.jsonValue(value) == "serialized_messages_and_tools_chars_div_4" ->
                        "Message and tool size, about 4 characters per token"
                    name == "requested_effort" && value == JsonNull -> "not requested"
                    else -> Fmt.jsonValue(value)
                }
                LabeledText(name.replace('_', ' '), text)
            }
        }
        Gap(12.dp)
        T("Applied model requirements", ty.heading, c.fg)
        Gap(6.dp)
        val requirements = choice.facts.requirements
        if (requirements == null) T("No custom requirements were reported.", ty.small, c.fgMuted)
        else FlowRow(horizontalArrangement = Arrangement.spacedBy(18.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            LabeledText("providers", requirements.providers.joinToString(", ").ifBlank { "any" })
            LabeledText("model types", requirements.modelTypes.joinToString(", ").ifBlank { "any" })
            LabeledText("capabilities", requirements.requiredCapabilities.joinToString(", ").replace('_', ' ').ifBlank { "any" })
            LabeledText("thinking level", requirements.thinkingLevel ?: "any")
            LabeledText("editing", requirementLabel(requirements.editable))
            LabeledText("tool calls", requirementLabel(requirements.toolCalls))
            LabeledText("minimum context", requirements.minContextTokens?.toString() ?: "any")
            LabeledText("price ceiling USD / M", requirements.maxUsdPerMtok?.let { Fmt.perMtok(it).text } ?: "router policy")
        }
    }
}

private fun requirementLabel(value: Boolean?) = when (value) {
    true -> "required"
    false -> "excluded"
    null -> "any"
}
