package dev.mrvishwet.adrd.ingest

import java.io.File
import java.io.RandomAccessFile
import kotlin.math.min

/**
 * Reads fixed-size slices out of a large file without ever holding the whole file.
 * Every read is random access (seek + one bounded read); the buffer is allocated at
 * exactly chunkSize, so peak read memory is chunkSize and nothing else.
 */
class ChunkReader(
    source: File,
    val chunkSize: Int = DEFAULT_CHUNK_SIZE,
) : AutoCloseable {
    init {
        require(chunkSize > 0) { "chunkSize must be positive, was $chunkSize" }
        require(source.exists()) { "source file does not exist: ${source.name}" }
    }

    private val raf = RandomAccessFile(source, "r")
    val sourceSize: Long = raf.length()
    val numChunks: Int =
        if (sourceSize == 0L) 0 else ((sourceSize + chunkSize - 1) / chunkSize).toInt()

    fun expectedLength(index: Int): Int {
        require(index in 0 until numChunks) {
            "chunk index $index outside 0..${numChunks - 1}"
        }
        val start = index.toLong() * chunkSize
        return min(chunkSize.toLong(), sourceSize - start).toInt()
    }

    /**
     * Returns the bytes of chunk [index]. For a full chunk this is exactly chunkSize
     * bytes with no second copy; only the final short chunk is trimmed.
     */
    fun readChunk(index: Int): ByteArray {
        val len = expectedLength(index)
        val buffer = ByteArray(chunkSize)
        raf.seek(index.toLong() * chunkSize)
        readFully(buffer, len)
        return if (len == chunkSize) buffer else buffer.copyOf(len)
    }

    private fun readFully(buffer: ByteArray, len: Int) {
        var got = 0
        while (got < len) {
            val n = raf.read(buffer, got, len - got)
            if (n < 0) break
            got += n
        }
        check(got == len) { "short read at chunk $len: got $got of $len" }
    }

    override fun close() {
        raf.close()
    }

    companion object {
        const val DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
    }
}
