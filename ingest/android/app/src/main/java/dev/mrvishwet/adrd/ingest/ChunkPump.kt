package dev.mrvishwet.adrd.ingest

import java.io.File

/**
 * Delivers one chunk and reports whether the server acked it. Returning false means
 * "not acked" (drop, retry later, already have it failed): the pump stops and the
 * checkpoint stays where it is, so this chunk is replayed, never skipped.
 */
fun interface ChunkAck {
    fun ack(index: Int, bytes: ByteArray): Boolean
}

/**
 * Wires ChunkReader to CheckpointStore: read chunk, hand it to the ack callback, and
 * advance the checkpoint ONLY after the ack succeeded. A crash or a failed ack
 * between read and ack therefore costs exactly one re-sent chunk.
 */
class ChunkPump(
    private val reader: ChunkReader,
    private val checkpointFile: File,
    private val uploadId: String,
    private val sourceSha256: String,
    private val sourceGenerationId: String,
    private val ack: ChunkAck,
) {
    fun currentCheckpoint(): Checkpoint? = CheckpointStore.load(checkpointFile)

    /** Runs from the recorded offset to the end, or to the first un-acked chunk. */
    fun run(): Checkpoint {
        var cp = currentCheckpoint() ?: Checkpoint(
            uploadId = uploadId,
            chunkSize = reader.chunkSize,
            nextIndex = 0,
            sourceSize = reader.sourceSize,
            sourceSha256 = sourceSha256,
            sourceGenerationId = sourceGenerationId,
        )
        check(cp.chunkSize == reader.chunkSize && cp.sourceSize == reader.sourceSize) {
            "checkpoint (chunkSize=${cp.chunkSize}, size=${cp.sourceSize}) does not " +
                "match this reader (chunkSize=${reader.chunkSize}, size=${reader.sourceSize})"
        }
        check(cp.uploadId == uploadId && cp.sourceGenerationId == sourceGenerationId) {
            "checkpoint belongs to a different upload/generation: " +
                "${cp.uploadId}/${cp.sourceGenerationId}"
        }
        var index = cp.nextIndex
        while (index < reader.numChunks) {
            val bytes = reader.readChunk(index)
            if (!ack.ack(index, bytes)) {
                return cp // not acked: next run replays this index
            }
            cp = cp.copy(nextIndex = index + 1)
            CheckpointStore.save(checkpointFile, cp)
            index++
        }
        return cp
    }
}
