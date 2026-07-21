import React from "react";
import { AbsoluteFill, Audio, Sequence, staticFile } from "remotion";
import { SceneMedia, MediaItem } from "./SceneMedia";
import { SyncedCaptions } from "./SyncedCaptions";
import { HookEffect } from "./HookEffect";
import { TRANSITION_FRAMES } from "./transitionConfig";

interface RawMediaItem extends MediaItem {
  startFrame: number;
  durationFrames: number;
}

interface WordEvent {
  word: string;
  start_ms: number;
  duration_ms: number;
}

interface Props {
  script: { hook: string; scenes: any[]; closing_cta: string };
  audioPath: string;
  captions: WordEvent[]; // بيانات حية بدل رابط
  mediaItems: RawMediaItem[];
  durationSeconds: number;
  width: number;
  height: number;
  fps: number;
}

export const MainVideo: React.FC<Props> = ({
  audioPath,
  captions,
  mediaItems,
  durationSeconds,
  fps,
  width,
  height,
}) => {
  const isShort = height > width;
  const totalFrames = durationSeconds * fps;

  const items = mediaItems && mediaItems.length > 0 ? mediaItems : [];

  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      {audioPath && <Audio src={staticFile(audioPath)} />}

      {items.map((item, i) => {
        const isFirst = i === 0;
        const isLast = i === items.length - 1;

        const renderFrom = isFirst
          ? item.startFrame
          : Math.max(0, item.startFrame - TRANSITION_FRAMES);
        const naturalEnd = item.startFrame + item.durationFrames;
        const renderEnd = isLast
          ? Math.min(totalFrames, naturalEnd)
          : Math.min(totalFrames, naturalEnd + TRANSITION_FRAMES);
        const renderDuration = Math.max(1, renderEnd - renderFrom);

        const fadeInFrames = isFirst ? 0 : item.startFrame - renderFrom;
        const fadeOutFrames = isLast ? 0 : renderEnd - naturalEnd;

        return (
          <Sequence key={i} from={renderFrom} durationInFrames={renderDuration}>
            <SceneMedia
              item={{ type: item.type, src: item.src }}
              seed={i}
              isShort={isShort}
              renderDuration={renderDuration}
              fadeInFrames={fadeInFrames}
              fadeOutFrames={fadeOutFrames}
            />
          </Sequence>
        );
      })}

      <AbsoluteFill
        style={{
          background: "linear-gradient(to top, rgba(0,0,0,0.65) 0%, rgba(0,0,0,0) 30%)",
        }}
      />

      {/* تمرير مصفوفة الكابشن مباشرة لتعمل فوراً بالرندرة */}
      <SyncedCaptions captions={captions || []} isShort={isShort} />

      {/* تأثير بصري "خطاف" في أول 3 ثوانٍ لجذب المشاهد */}
      <HookEffect isShort={isShort} />
    </AbsoluteFill>
  );
};
