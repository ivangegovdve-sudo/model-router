package modelrouter.app

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

@Serializable
data class PlanBinding(
    val kind: String = "auto",
    val seat: String? = null,
    @SerialName("executor_id") val executorId: String? = null,
    @SerialName("model_ref") val modelRef: String? = null,
)

@Serializable
data class PlanEstimate(
    @SerialName("prompt_tokens") val promptTokens: Int? = null,
    @SerialName("answer_tokens") val answerTokens: Int? = null,
    @SerialName("max_tokens") val maxTokens: Int? = null,
)

@Serializable
data class PlanSlot(
    val id: String,
    val role: String? = null,
    val roles: List<String> = emptyList(),
    val binding: PlanBinding = PlanBinding(),
    val requirements: RouterRequirements? = null,
    val estimate: PlanEstimate? = null,
    val lane: String? = null,
)

@Serializable
data class PlanReference(
    val id: String,
    val models: List<String> = emptyList(),
    val revision: String = "",
    @SerialName("evidence_id") val evidenceId: String? = null,
)

@Serializable
data class PlanPeer(val slot: String? = null, val reference: String? = null)

@Serializable
@OptIn(ExperimentalSerializationApi::class)
data class PlanConstraint(
    val id: String,
    val kind: String = "all_distinct",
    val dimension: String = "weights",
    @EncodeDefault(EncodeDefault.Mode.NEVER) val slots: List<String> = emptyList(),
    val slot: String? = null,
    @EncodeDefault(EncodeDefault.Mode.NEVER) val others: List<PlanPeer> = emptyList(),
)

@Serializable
data class PlanTemplateRef(val id: String, val version: String)

@Serializable
data class SeatPlanRequest(
    val schema: String = "seat-plan/1",
    val consumer: String,
    val policy: String? = null,
    val template: PlanTemplateRef? = null,
    val context: JsonObject = JsonObject(emptyMap()),
    val slots: List<PlanSlot>,
    val references: List<PlanReference> = emptyList(),
    val constraints: List<PlanConstraint> = emptyList(),
)

@Serializable
data class SeatPlanTemplate(
    val id: String,
    val version: String = "1",
    val name: String = "",
    val consumer: String,
    val context: JsonObject = JsonObject(emptyMap()),
    val slots: List<PlanSlot> = emptyList(),
    val references: List<PlanReference> = emptyList(),
    val constraints: List<PlanConstraint> = emptyList(),
)

@Serializable
data class SeatPlanTemplates(val schema: String = "seat-plan/1", val templates: List<SeatPlanTemplate> = emptyList())

@Serializable
data class PlanAssignment(
    @SerialName("slot_id") val slotId: String = "",
    val role: String? = null,
    val roles: List<String> = emptyList(),
    val binding: PlanBinding = PlanBinding(),
    val seat: String? = null,
    val provider: String? = null,
    val model: String? = null,
    @SerialName("weights_id") val weightsId: String? = null,
    @SerialName("identity_evidence_id") val identityEvidenceId: String? = null,
    val family: String? = null,
    val requirements: RouterRequirements? = null,
    val lane: String? = null,
    @SerialName("estimated_usd") val estimatedUsd: String? = null,
    val invoke: JsonObject? = null,
    val state: String = "PENDING",
)

@Serializable
data class PlanPairEvidence(
    val left: PlanPeer = PlanPeer(),
    val right: PlanPeer = PlanPeer(),
    @SerialName("left_weights_id") val leftWeightsId: String? = null,
    @SerialName("right_weights_ids") val rightWeightsIds: List<String> = emptyList(),
    val satisfied: Boolean? = null,
)

@Serializable
data class PlanConstraintEvidence(
    val id: String = "",
    val kind: String = "",
    val dimension: String = "weights",
    val satisfied: Boolean? = null,
    val pairs: List<PlanPairEvidence> = emptyList(),
)

@Serializable
data class PlanUnknown(
    @SerialName("slot_id") val slotId: String? = null,
    @SerialName("reference_id") val referenceId: String? = null,
    val seat: String? = null,
    val facts: List<String> = emptyList(),
    val because: String = "",
)

@Serializable
data class PlanConsideration(
    @SerialName("slot_id") val slotId: String = "",
    val seat: String? = null,
    @SerialName("executor_id") val executorId: String? = null,
    val verdict: String = "UNKNOWN",
    val because: String = "",
    val unknown: List<String> = emptyList(),
    @SerialName("weights_id") val weightsId: String? = null,
    @SerialName("estimated_usd") val estimatedUsd: String? = null,
)

@Serializable
data class SeatPlanResponse(
    val schema: String = "seat-plan/1",
    val outcome: String = "UNKNOWN",
    val because: String = "",
    @SerialName("plan_id") val planId: String? = null,
    val version: Int? = null,
    @SerialName("snapshot_id") val snapshotId: String? = null,
    val consumer: String = "",
    val context: JsonObject = JsonObject(emptyMap()),
    val assignments: List<PlanAssignment> = emptyList(),
    @SerialName("constraint_evidence") val constraintEvidence: List<PlanConstraintEvidence> = emptyList(),
    val unknown: List<PlanUnknown> = emptyList(),
    val considered: List<PlanConsideration> = emptyList(),
    val estimates: JsonObject = JsonObject(emptyMap()),
    val references: Map<String, JsonElement> = emptyMap(),
)
