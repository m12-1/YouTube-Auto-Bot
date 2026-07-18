import React from "react";
import { AbsoluteFill, Audio, Sequence } from "remotion";
import { KenBurnsImage } from "./KenBurnsImage";
import { SyncedCaptions } from "./SyncedCaptions";

interface Props {
  script: { hook: string; scenes: any[]; closing_cta: string };
  audioPath: string;
  captionsPath: string;
  imagePaths: string[];
  durationSeconds: number;
  width: number;
  height: number;
  fps: number;
}

/**
 * يوزّع الصور على مدة الفيديو بالتساوي مع Ken Burns لكل صورة،
 * وطبقة كابشن فوقها مزامنة بالصوت.
 */
export const MainVideo: React.FC<Props> = ({
  audioPath,
  captionsPath,
  imagePaths,
  durationSeconds,
  fps,
  width,
  height,
}) => {
  const isShort = height > width; 
  const totalFrames = durationSeconds * fps;
  const secondsPerImage = isShort ? 2 : 2.5;
  const framesPerImage = Math.floor(secondsPerImage * fps);
  const imageCount = Math.max(1, Math.floor(totalFrames / framesPerImage));

  const captions: any[] = []; 

  // إضافة file:// للمسار المطلق حتى يقرأه Remotion من نظام التشغيل مباشرة
  const audioSrc = audioPath.startsWith("/") ? `file://${audioPath}` : audioPath;

  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      <Audio src={audioSrc} />

      {Array.from({ length: imageCount }).map((_, i) => {
        const src = imagePaths[i % imagePaths.length];
        const startFrame = i * framesPerImage;
        return (
          <Sequence key={i} from={startFrame} durationInFrames={framesPerImage}>
            <KenBurnsImage
              src={src}
              startFrame={startFrame}
              durationInFrames={framesPerImage}
              seed={i}
              isShort={isShort}
            />
          </Sequence>
        );
      })}

      {/* تدرّج داكن أسفل الفيديو لضمان وضوح الكابشن فوق أي خلفية */}
      <AbsoluteFill
        style={{
          background: "linear-gradient(to top, rgba(0,0,0,0.65) 0%, rgba(0,0,0,0) 30%)",
        }}
      />

      <SyncedCaptions captions={captions} isShort={isShort} />
    </AbsoluteFill>
  );
};
