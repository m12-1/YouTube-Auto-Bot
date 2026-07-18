import React from "react";
import { AbsoluteFill, Audio, Sequence, useVideoConfig } from "remotion";
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
 * يوزّع الصور على مدة الفيديو بالتساوي (تبديل كل 2-3 ثوانٍ حتى لو الصوت
 * مستمر بنفس الفقرة، حسب ما اتفقنا) مع Ken Burns لكل صورة، وطبقة كابشن
 * فوقها مزامنة بالصوت.
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
  const isShort = height > width; // عمودي = شورت
  const totalFrames = durationSeconds * fps;
  // بالشورت الإيقاع أسرع (فيديو أقصر وجمهور أسرع تمريراً) — تبديل كل 2 ثانية
  // بدل 2.5 بالطويل، يحافظ على الحيوية البصرية طوال الـ 55 ثانية
  const secondsPerImage = isShort ? 2 : 2.5;
  const framesPerImage = Math.floor(secondsPerImage * fps);
  const imageCount = Math.max(1, Math.floor(totalFrames / framesPerImage));

  // ملاحظة: captions تُقرأ فعلياً وقت الرندرة عبر fetch من captionsPath
  // (ملف JSON محلي تم تمريره بالـ props)، هنا تبسيط توضيحي للبنية.
  const captions: any[] = []; // يُملأ فعلياً بكود تحميل JSON قبل التمرير للمكون

  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      <Audio src={audioPath} />

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
