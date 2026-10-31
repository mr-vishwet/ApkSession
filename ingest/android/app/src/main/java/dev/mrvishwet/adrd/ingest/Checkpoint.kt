package dev.mrvishwet.adrd.ingest

import java.io.File
import java.io.FileOutputStream

/**
 * Resume state for one upload. Written only AFTER the server acked the chunk it
 * points past, so it always describes completed work, never work in flight.
 */
data class Checkpoint(
    val uploadId: String,
    val chunkSize: Int,
    val nextIndex: Int,
    val sourceSize: Long,
    val sourceSha256: String,
    val sourceGenerationId: String,
)

/**
 * Atomic checkpoint persistence: write a temp file, flush, fsync, then rename over
 * the target. Rename is atomic on Linux/Android, so a load() can only ever observe
 * a complete previous record or the complete new one - never a torn one.
 */
object CheckpointStore {
    private val KEYS = listOf(
        "upload_id", "chunk_size", "next_index", "source_size",
        "source_sha256", "source_generation_id",
    )

    fun save(file: File, cp: Checkpoint) {
        listOf(cp.uploadId, cp.sourceSha256, cp.sourceGenerationId).forEach {
            require(!it.contains('\n') && !it.contains('=')) {
                "checkpoint string fields must not contain newline or ="
            }
        }
        require(cp.nextIndex >= 0 && cp.chunkSize > 0 && cp.sourceSize >= 0) {
            "checkpoint numbers must be non-negative (chunkSize positive)"
        }
        val dir = file.absoluteFile.parentFile
        dir.mkdirs()
        val tmp = File(dir, file.name + ".tmp." + System.nanoTime())
        FileOutputStream(tmp).use { fos ->
            val text = buildString {
                append("upload_id=").append(cp.uploadId).append('\n')
                append("chunk_size=").append(cp.chunkSize).append('\n')
                append("next_index=").append(cp.nextIndex).append('\n')
                append("source_size=").append(cp.sourceSize).append('\n')
                append("source_sha256=").append(cp.sourceSha256).append('\n')
                append("source_generation_id=").append(cp.sourceGenerationId).append('\n')
            }
            fos.write(text.toByteArray(Charsets.UTF_8))
            fos.flush()
            fos.fd.sync()
        }
        if (!tmp.renameTo(file)) {
            tmp.delete()
            throw java.io.IOException("checkpoint rename failed for ${file.name}")
        }
    }

    /** null when no checkpoint exists; throws if a checkpoint exists but is malformed. */
    fun load(file: File): Checkpoint? {
        if (!file.exists()) return null
        val values = mutableMapOf<String, String>()
        file.readText(Charsets.UTF_8).lineSequence().forEach { line ->
            val i = line.indexOf('=')
            if (i > 0) values[line.substring(0, i)] = line.substring(i + 1)
        }
        val missing = KEYS.filter { !values.containsKey(it) }
        if (missing.isNotEmpty()) {
            throw IllegalStateException("checkpoint missing keys: $missing")
        }
        return Checkpoint(
            uploadId = values.getValue("upload_id"),
            chunkSize = values.getValue("chunk_size").toIntOrNull()
                ?: throw IllegalStateException("bad chunk_size"),
            nextIndex = values.getValue("next_index").toIntOrNull()
                ?: throw IllegalStateException("bad next_index"),
            sourceSize = values.getValue("source_size").toLongOrNull()
                ?: throw IllegalStateException("bad source_size"),
            sourceSha256 = values.getValue("source_sha256"),
            sourceGenerationId = values.getValue("source_generation_id"),
        )
    }
}
