package dev.mrvishwet.adrd.ingest

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.RandomAccessFile
import java.security.MessageDigest
import java.util.Random

class ChunkReaderTest {
    @JvmField
    @Rule
    val folder = TemporaryFolder()

    private val MiB = 1024 * 1024

    /** Deterministic patterned file: 8 KiB blocks from a seeded Random. */
    private fun makeFile(name: String, size: Long, seed: Long = 42L): File {
        val f = File(folder.root, name)
        if (f.exists()) f.delete()
        val rnd = Random(seed)
        java.io.FileOutputStream(f).use { out ->
            val block = ByteArray(8192)
            var written = 0L
            while (written < size) {
                rnd.nextBytes(block)
                val n = minOf(block.size.toLong(), size - written).toInt()
                out.write(block, 0, n)
                written += n
            }
        }
        return f
    }

    private fun sha256Stream(f: File): ByteArray {
        val md = MessageDigest.getInstance("SHA-256")
        FileInputStream(f).use { input ->
            val buf = ByteArray(65536)
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest()
    }

    private fun hex(bytes: ByteArray): String =
        bytes.joinToString("") { (it.toInt() and 0xFF).toString(16).padStart(2, '0') }

    private fun usedMb(): Long = with(Runtime.getRuntime()) {
        System.gc()
        Thread.sleep(60)
        (totalMemory() - freeMemory()) / 1024 / 1024
    }

    @Test
    fun sequentialReadsConcatenatedEqualTheSourceFile() {
        val size = 64L * MiB + 5000
        val f = makeFile("big.bin", size)
        ChunkReader(f).use { reader ->
            assertEquals(ChunkReader.DEFAULT_CHUNK_SIZE, reader.chunkSize)
            assertEquals(4 * MiB, reader.chunkSize)
            assertEquals(17, reader.numChunks)
            val md = MessageDigest.getInstance("SHA-256")
            for (i in 0 until reader.numChunks) {
                val b = reader.readChunk(i)
                assertEquals(reader.expectedLength(i), b.size)
                if (i < 16) assertEquals(4 * MiB, b.size) // bounded by construction
                md.update(b)
            }
            assertEquals(5000, reader.expectedLength(16))
            assertEquals(hex(sha256Stream(f)), hex(md.digest()))
        }
    }

    @Test
    fun resumeFromCheckpointStartsAtTheRecordedOffset() {
        val cs = MiB
        val size = 10L * cs + 7
        val f = makeFile("resume.bin", size)
        val cpFile = File(folder.root, "up.checkpoint")
        val sha = hex(sha256Stream(f))
        CheckpointStore.save(
            cpFile, Checkpoint("up1", cs, 3, size, sha, "gen-1")
        )
        val acked = mutableListOf<Int>()
        val md = MessageDigest.getInstance("SHA-256")
        ChunkReader(f, cs).use { reader ->
            val pump = ChunkPump(reader, cpFile, "up1", sha, "gen-1") { i, bytes ->
                acked += i
                md.update(bytes)
                true
            }
            val end = pump.run()
            assertEquals(reader.numChunks, end.nextIndex)
        }
        assertEquals((3 until 11).toList(), acked) // chunks 0..2 are never re-read
        // the remainder must equal the source bytes from offset 3*cs on
        val tail = MessageDigest.getInstance("SHA-256")
        RandomAccessFile(f, "r").use { raf ->
            raf.seek(3L * cs)
            val buf = ByteArray(65536)
            var left = size - 3L * cs
            while (left > 0) {
                val n = raf.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
                if (n < 0) break
                tail.update(buf, 0, n)
                left -= n
            }
        }
        assertEquals(hex(tail.digest()), hex(md.digest()))
    }

    @Test
    fun crashOrDropBetweenReadAndAckReplaysThatChunk() {
        val cs = 1024
        val f = makeFile("crash.bin", 5L * cs)
        val cpFile = File(folder.root, "crash.checkpoint")
        val sha = hex(sha256Stream(f))
        val seen = mutableListOf<Int>()
        ChunkReader(f, cs).use { reader ->
            // first attempt: ack throws after the chunk was read (simulated crash)
            val boom = ChunkPump(reader, cpFile, "up2", sha, "gen-2") { i, _ ->
                seen += i
                if (i == 2) throw IllegalStateException("process died here")
                true
            }
            var thrown = false
            try {
                boom.run()
            } catch (_: IllegalStateException) {
                thrown = true
            }
            assertTrue("ack crash must propagate", thrown)
            assertEquals(2, CheckpointStore.load(cpFile)!!.nextIndex)
            // resume: chunk 2 replays (not skipped), then the rest completes
            seen.clear()
            val resume = ChunkPump(reader, cpFile, "up2", sha, "gen-2") { i, _ ->
                seen += i
                true
            }
            val end = resume.run()
            assertEquals(5, end.nextIndex)
            assertEquals(listOf(2, 3, 4), seen)
        }
    }

    @Test
    fun failedAckStopsWithoutAdvancingTheCheckpoint() {
        val cs = 1024
        val f = makeFile("drop.bin", 4L * cs)
        val cpFile = File(folder.root, "drop.checkpoint")
        val sha = hex(sha256Stream(f))
        ChunkReader(f, cs).use { reader ->
            var n = 0
            val pump = ChunkPump(reader, cpFile, "up3", sha, "gen-3") { _, _ ->
                n++
                n < 3 // third delivery fails (network drop)
            }
            val cp = pump.run()
            assertEquals(2, cp.nextIndex)
            assertEquals(2, CheckpointStore.load(cpFile)!!.nextIndex)
        }
    }

    @Test
    fun checkpointReloadAfterCrashedWriterIsNeverTorn() {
        val cpFile = File(folder.root, "atomic.checkpoint")
        val first = Checkpoint("up4", 4096, 5, 99L * 4096, "aa".repeat(32), "gen-4")
        CheckpointStore.save(cpFile, first)
        // a crashed writer leaves a half-written temp file; the target must stay intact
        val torn = File(folder.root, "atomic.checkpoint.tmp.999")
        torn.writeText("upload_id=up4\nchunk_size=4096\nnext_index=9\nsou")
        val loaded = CheckpointStore.load(cpFile)
        assertEquals(first, loaded) // consistent last-completed, never the torn data
        assertNull(CheckpointStore.load(File(folder.root, "does-not-exist")))
        val second = first.copy(nextIndex = 7)
        CheckpointStore.save(cpFile, second)
        assertEquals(second, CheckpointStore.load(cpFile))
        assertTrue(torn.exists()) // stray temps are ignored, not read
    }

    @Test
    fun peakReadBufferEqualsConfiguredChunkSize() {
        val cs = 256 * 1024
        val f = makeFile("bounded.bin", 3L * cs + 100)
        ChunkReader(f, cs).use { reader ->
            val b = reader.readChunk(0)
            assertEquals(cs, b.size) // buffer is allocated at exactly chunkSize
            val last = reader.readChunk(3)
            assertEquals(100, last.size)
        }
    }

    @Test
    fun sparseTwoGigFileStaysUnderSixteenMbOfHeapWhileReading() {
        val f = File(folder.root, "sparse.bin")
        RandomAccessFile(f, "rw").use { it.setLength(2L * 1024 * MiB) }
        val before = usedMb()
        ChunkReader(f, 4 * MiB).use { reader ->
            assertEquals(512, reader.numChunks)
            var live: ByteArray? = null
            for (i in listOf(0, 127, 256, 511)) {
                live = reader.readChunk(i)
            }
            val after = usedMb()
            assertEquals(4 * MiB, live!!.size)
            assertTrue(
                "peak heap grew ${after - before} MB; must stay under 16 MB",
                after - before < 16
            )
        }
        f.delete()
    }

    @Test
    fun zeroByteFileHasNoChunksAndCompletesImmediately() {
        val f = makeFile("empty.bin", 0)
        val cpFile = File(folder.root, "empty.checkpoint")
        ChunkReader(f, MiB).use { reader ->
            assertEquals(0, reader.numChunks)
            val pump = ChunkPump(reader, cpFile, "up5", hex("".toByteArray()), "gen-5") { _, _ ->
                throw AssertionError("never called for an empty file")
            }
            assertEquals(0, pump.run().nextIndex)
        }
    }
}
