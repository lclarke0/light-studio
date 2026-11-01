package com.lightstudio.app.core;

import java.io.File;

/** Downloads the audio of one YouTube video into a folder. */
public interface YouTubeBackend {
    interface Progress {
        void message(String msg);
    }

    final class Result {
        public final String title;
        public final double duration;
        public final File file;
        public final String source;

        public Result(String title, double duration, File file, String source) {
            this.title = title;
            this.duration = duration;
            this.file = file;
            this.source = source;
        }
    }

    /** maxSeconds: refuse longer videos. The file must be written inside dir as audio.<ext>. */
    Result fetch(String url, File dir, long maxSeconds, long maxBytes, Progress progress) throws Exception;
}
