package com.lightstudio.app;

import com.lightstudio.app.core.YouTubeBackend;

import org.schabi.newpipe.extractor.MediaFormat;
import org.schabi.newpipe.extractor.NewPipe;
import org.schabi.newpipe.extractor.ServiceList;
import org.schabi.newpipe.extractor.downloader.Downloader;
import org.schabi.newpipe.extractor.downloader.Request;
import org.schabi.newpipe.extractor.downloader.Response;
import org.schabi.newpipe.extractor.exceptions.ReCaptchaException;
import org.schabi.newpipe.extractor.services.youtube.YoutubeParsingHelper;
import org.schabi.newpipe.extractor.stream.AudioStream;
import org.schabi.newpipe.extractor.stream.DeliveryMethod;
import org.schabi.newpipe.extractor.stream.StreamInfo;
import org.schabi.newpipe.extractor.stream.StreamType;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** YouTube audio download using the NewPipe extractor library. */
final class NewPipeYouTube implements YouTubeBackend {
    private static final String FIREFOX_UA =
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0";
    private static boolean initialized;

    private static synchronized void init() {
        if (!initialized) {
            NewPipe.init(new Dl());
            initialized = true;
        }
    }

    /** HTTP for the extractor. */
    private static final class Dl extends Downloader {
        @Override
        public Response execute(Request request) throws IOException, ReCaptchaException {
            HttpURLConnection c = (HttpURLConnection) new URL(request.url()).openConnection();
            c.setConnectTimeout(15000);
            c.setReadTimeout(30000);
            c.setInstanceFollowRedirects(true);
            c.setRequestMethod(request.httpMethod());
            c.setRequestProperty("User-Agent", FIREFOX_UA);
            for (Map.Entry<String, List<String>> h : request.headers().entrySet()) {
                boolean first = true;
                for (String v : h.getValue()) {
                    if (first) c.setRequestProperty(h.getKey(), v);
                    else c.addRequestProperty(h.getKey(), v);
                    first = false;
                }
            }
            byte[] data = request.dataToSend();
            if (data != null) {
                c.setDoOutput(true);
                try (OutputStream o = c.getOutputStream()) { o.write(data); }
            }
            int code = c.getResponseCode();
            if (code == 429) throw new ReCaptchaException("YouTube is asking for a captcha — try again later", request.url());
            InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
            String body = "";
            if (in != null) {
                try (InputStream i = in) {
                    ByteArrayOutputStream b = new ByteArrayOutputStream();
                    byte[] buf = new byte[16384];
                    int n;
                    while ((n = i.read(buf)) > 0) b.write(buf, 0, n);
                    body = b.toString(StandardCharsets.UTF_8.name());
                }
            }
            Map<String, List<String>> headers = new HashMap<>();
            for (Map.Entry<String, List<String>> h : c.getHeaderFields().entrySet())
                if (h.getKey() != null) headers.put(h.getKey(), h.getValue());
            return new Response(code, c.getResponseMessage(), headers, body, c.getURL().toString());
        }
    }

    private static String userAgentFor(String url) {
        if (YoutubeParsingHelper.isAndroidStreamingUrl(url)) return YoutubeParsingHelper.getAndroidUserAgent(null);
        if (YoutubeParsingHelper.isIosStreamingUrl(url)) return YoutubeParsingHelper.getIosUserAgent(null);
        if (YoutubeParsingHelper.isVisionOsStreamingUrl(url)) return YoutubeParsingHelper.getVisionOsUserAgent(null);
        return FIREFOX_UA;
    }

    @Override
    public Result fetch(String url, File dir, long maxSeconds, long maxBytes, Progress progress) throws Exception {
        init();
        progress.message("Looking up the video…");
        StreamInfo info;
        try {
            info = StreamInfo.getInfo(ServiceList.YouTube, url);
        } catch (Exception e) {
            throw new IOException("couldn't read that video (" + e.getMessage() + ")", e);
        }
        StreamType type = info.getStreamType();
        if (type == StreamType.LIVE_STREAM || type == StreamType.AUDIO_LIVE_STREAM)
            throw new IOException("live streams can't be downloaded");
        if (info.getDuration() > maxSeconds) throw new IOException("the video is longer than " + (maxSeconds / 60) + " minutes");

        AudioStream best = null;
        for (AudioStream s : info.getAudioStreams()) {
            if (s.getDeliveryMethod() != DeliveryMethod.PROGRESSIVE_HTTP || !s.isUrl()) continue;
            if (best == null) { best = s; continue; }
            boolean m4a = s.getFormat() == MediaFormat.M4A, bestM4a = best.getFormat() == MediaFormat.M4A;
            if ((m4a && !bestM4a) || (m4a == bestM4a && s.getAverageBitrate() > best.getAverageBitrate())) best = s;
        }
        if (best == null) throw new IOException("couldn't find a downloadable audio track for this video");
        String ext = best.getFormat() == MediaFormat.M4A ? ".m4a" : ".webm";
        File out = new File(dir, "audio" + ext);
        download(best.getContent(), out, maxBytes, progress);
        return new Result(info.getName(), info.getDuration(), out, info.getUrl());
    }

    private static void download(String streamUrl, File out, long maxBytes, Progress progress) throws IOException {
        String ua = userAgentFor(streamUrl);
        Matcher clen = Pattern.compile("[?&]clen=(\\d+)").matcher(streamUrl);
        long total = clen.find() ? Long.parseLong(clen.group(1)) : -1;
        if (total > maxBytes) throw new IOException("the audio is too large");
        final long chunk = 2L << 20;
        long pos = 0;
        progress.message("Downloading audio…");
        try (OutputStream o = new FileOutputStream(out)) {
            while (total < 0 || pos < total) {
                long end = total > 0 ? Math.min(total, pos + chunk) - 1 : pos + chunk - 1;
                String u = total > 0 ? streamUrl + "&range=" + pos + "-" + end : streamUrl;
                HttpURLConnection c = (HttpURLConnection) new URL(u).openConnection();
                c.setConnectTimeout(15000);
                c.setReadTimeout(30000);
                c.setRequestProperty("User-Agent", ua);
                if (total <= 0) c.setRequestProperty("Range", "bytes=" + pos + "-" + end);
                int code = c.getResponseCode();
                if (code != 200 && code != 206) throw new IOException("YouTube refused the download (HTTP " + code + ")");
                if (total <= 0) {
                    String cr = c.getHeaderField("Content-Range");
                    if (code == 206 && cr != null && cr.contains("/")) {
                        try { total = Long.parseLong(cr.substring(cr.indexOf('/') + 1).trim()); } catch (NumberFormatException ignored) { }
                    }
                }
                long got = 0;
                try (InputStream in = c.getInputStream()) {
                    byte[] buf = new byte[65536];
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        o.write(buf, 0, n);
                        got += n;
                        pos += n;
                        if (pos > maxBytes) throw new IOException("the audio is too large");
                    }
                }
                if (total > 0) progress.message("Downloading audio… " + (pos * 100 / total) + "%");
                else progress.message("Downloading audio… " + (pos >> 20) + " MB");
                if (got == 0 || (code == 200 && total <= 0)) break;   // server sent everything at once
                if (total <= 0) total = pos;                           // unknown size and no range support
            }
        }
        if (pos == 0) throw new IOException("the download was empty");
        progress.message("Download finished, adding to your library…");
    }
}
