package dev.mrvishwet.adrd.ingest

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Host-JVM tests for US-004. The picker itself can only be proven on device, but every
 * decision made about a picked clip - content URI or not, what the generation id is, how
 * the clip reads in the list - is exercised here against a fake source that answers for
 * real device clip URIs.
 */
class ClipResolverTest {

    private class FakeClipSource(var fields: ClipFields) : ClipDataSource {
        var askedUri: String? = null
        override fun read(uri: String): ClipFields {
            askedUri = uri
            return fields
        }
    }

    /**
     * Exactly what the demo35 stock clip reports: 3840x2160 hev1 in an mp4 container,
     * ~40 Mbps, 50,357,449 B. Note the container mime is video/mp4 - only the track
     * mime says hevc.
     */
    private val stock4k = ClipFields(
        displayName = "stock4k_10s.mp4",
        sizeBytes = 50_357_449L,
        lastModifiedSeconds = 1_762_000_000L,
        containerMime = "video/mp4",
        videoCodecMime = "video/hevc",
        width = 3840,
        height = 2160,
        bitrateBps = 40_283_760L,
    )

    private val stockUri = "content://media/external/video/media/1000000019"

    @Test
    fun resolvesContentUriIntoDisplayableClip() {
        val source = FakeClipSource(stock4k)
        val clip = ClipResolver(source).resolve(stockUri)

        assertEquals(stockUri, source.askedUri)
        assertEquals(stockUri, clip.uri)
        assertEquals("stock4k_10s.mp4", clip.displayName)
        assertEquals("HEVC", clip.codecLabel)
        assertEquals("3840x2160", clip.resolution)
        assertEquals(40_283_760L, clip.bitrateBps)
        assertEquals(50_357_449L, clip.sizeBytes)
        assertTrue("4K HEVC clip must be identifiable from codec + resolution", clip.isHevc)
        assertTrue(clip.isFourK)
        assertEquals("sz50357449-mt1762000000", clip.generationId)
    }

    @Test
    fun refusesAPlainFilePath() {
        val source = FakeClipSource(stock4k)
        val e = assertThrows(ClipResolutionException::class.java) {
            ClipResolver(source).resolve("/sdcard/DCIM/Camera/stock4k_10s.mp4")
        }
        assertTrue(e.message!!.contains("content://"))
        assertEquals(null, source.askedUri)
    }

    @Test
    fun generationIdChangesWhenTheUnderlyingFileChanges() {
        val resolver = ClipResolver(FakeClipSource(stock4k))
        val before = resolver.resolve(stockUri)

        val bigger = stock4k.copy(sizeBytes = stock4k.sizeBytes + 1)
        val touched = stock4k.copy(lastModifiedSeconds = 1_762_000_001L)
        val reWritten = ClipResolver(FakeClipSource(bigger)).resolve(stockUri)
        val rewrittenSameSize = ClipResolver(FakeClipSource(touched)).resolve(stockUri)

        assertNotEquals("re-encode changes size, generation must move", before.generationId, reWritten.generationId)
        assertNotEquals(
            "a same-size rewrite that moves mtime must still be noticed",
            before.generationId,
            rewrittenSameSize.generationId,
        )
        assertEquals(
            "same file read twice must be stable",
            before.generationId,
            ClipResolver(FakeClipSource(stock4k)).resolve(stockUri).generationId,
        )
    }

    @Test
    fun generationIdStillWorksWhenTheProviderHidesTheDate() {
        val clip = ClipResolver(FakeClipSource(stock4k.copy(lastModifiedSeconds = null)))
            .resolve("content://media/picker/0/com.android.providers.media.module/1000000019")
        assertEquals("sz50357449", clip.generationId)
    }

    @Test
    fun sourceFailureIsReportedAsAClipResolutionFailure() {
        val boom = ClipResolver(object : ClipDataSource {
            override fun read(uri: String): ClipFields = throw IllegalStateException("retriever died")
        })
        val e = assertThrows(ClipResolutionException::class.java) { boom.resolve(stockUri) }
        assertEquals("retriever died", e.cause?.message)
    }

    @Test
    fun mediaStoreIdComesFromBothUriShapes() {
        assertEquals(1000000019L, ClipUri.mediaStoreId(stockUri))
        assertEquals(
            1000000022L,
            ClipUri.mediaStoreId(
                "content://com.google.android.providers.media.module/document/external:video:1000000022"
            ),
        )
        assertEquals(7L, ClipUri.mediaStoreId("content://media/external/video/media/7?name=clip"))
        assertEquals(null, ClipUri.mediaStoreId("content://media/external/video/media"))
        assertEquals(null, ClipUri.mediaStoreId("file:///sdcard/a.mp4"))
    }

    @Test
    fun formatsTheNumbersShownInList() {
        assertEquals("48.0 MiB", ClipFormat.size(50_357_449L))
        assertEquals("0 B", ClipFormat.size(0L))
        assertEquals("unknown", ClipFormat.size(-1L))
        assertEquals("40.3 Mbps", ClipFormat.bitrate(40_283_760L))
        assertEquals("unknown", ClipFormat.bitrate(0L))
        assertEquals("HEVC", ClipFormat.codecLabel("video/hevc"))
        assertEquals("H.264", ClipFormat.codecLabel("video/avc"))
        assertEquals("video/quicktime", ClipFormat.codecLabel("video/quicktime"))
        assertEquals("unknown", ClipFormat.codecLabel(""))
    }

    @Test
    fun pickedClipsSurviveARestart() {
        val file = File.createTempFile("picked_clips", ".txt").apply { deleteOnExit() }
        val resolver = ClipResolver(FakeClipSource(stock4k))
        val h264 = ClipResolver(FakeClipSource(stock4k.copy(
            displayName = "stockh264_1080p.mp4",
            sizeBytes = 3_018_207L,
            videoCodecMime = "video/avc",
            width = 1920,
            height = 1080,
            bitrateBps = 4_019_550L,
        ))).resolve("content://media/external/video/media/1000000022")
        val picked = listOf(resolver.resolve(stockUri), h264)

        ClipStore.save(file, picked)
        val reloaded = ClipStore.load(file)

        assertEquals(picked, reloaded)
        assertFalse("1080p H.264 clip must not read as 4K", reloaded[1].isFourK)
        assertFalse(reloaded[1].isHevc)
        assertTrue(reloaded[0].isFourK)
        assertEquals("HEVC", reloaded[0].codecLabel)
        assertEquals("H.264", reloaded[1].codecLabel)
        assertTrue(ClipStore.load(File(file.parent, "absent.txt")).isEmpty())
    }

    @Test
    fun clipStoreRefusesValuesThatWouldBreakTheRecordFormat() {
        val file = File.createTempFile("picked_clips_bad", ".txt").apply { deleteOnExit() }
        val bad = ClipResolver(FakeClipSource(stock4k.copy(displayName = "a=b")))
            .resolve(stockUri)
        assertThrows(IllegalArgumentException::class.java) { ClipStore.save(file, listOf(bad)) }
    }

    /**
     * The measured device behaviour this story hinged on: MediaMetadataRetriever calls an
     * HEVC clip "video/mp4", so the list is only honest about the codec if the track mime
     * wins over the container mime.
     */
    @Test
    fun trackCodecWinsOverTheContainerItShipsIn() {
        val hevc = ClipResolver(FakeClipSource(stock4k)).resolve(stockUri)
        assertEquals("video/hevc", hevc.codecMime)
        assertTrue(hevc.isHevc)

        val containerOnly = ClipResolver(FakeClipSource(stock4k.copy(videoCodecMime = null)))
            .resolve(stockUri)
        assertEquals("video/mp4", containerOnly.codecMime)
        assertEquals("MPEG-4", containerOnly.codecLabel)
        assertFalse("a clip whose track could not be read must not claim HEVC", containerOnly.isHevc)

        val blankTrack = ClipResolver(FakeClipSource(stock4k.copy(videoCodecMime = "  ")))
            .resolve(stockUri)
        assertEquals("video/mp4", blankTrack.codecMime)
    }

    @Test
    fun realCameraNameBeatsTheIdDerivedPickerName() {
        val pickerUri = "content://media/picker/0/com.android.providers.media.photopicker/media/1000000022"
        val id = ClipUri.mediaStoreId(pickerUri)
        assertEquals(1000000022L, id)
        assertTrue(ClipUri.prefersRowName("1000000022.mp4", id))
        assertFalse(ClipUri.prefersRowName("stockh264_1080p.mp4", id))
        assertFalse(ClipUri.prefersRowName("IMG_0045.mp4", null))
        assertTrue(ClipUri.prefersRowName(null, id))
    }
}
