import React, { useEffect, useState } from "react";
import {
  AbsoluteFill, Audio, Sequence, staticFile,
  continueRender, delayRender,
} from "remotion";
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
 * إصلاح جوនري بهذه النسخة: بالنسخة السابقة كان `captions` مصفوفة فارغة
 * ثابتة بالكود مباشرة (`const captions: any[] = []`)، يعني الكابشن ما كان
 * يظهر إطلاقاً بأي فيديو تم رندرته رغم نجاح الرندرة نفسها بدون خطأ ظاهر —
 * خطأ صامت خطير. الحل: تحميل captionsPath فعلياً عبر fetch + delayRender/
 * continueRender (الطريقة الرسمية بـ Remotion لانتظار بيانات غير متزامنة
 * قبل بدء الرندرة الفعلية لكل فريم).
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

  const [handle] = useState(() => delayRender("جاري تحميل ملف الكابشن JSON"));
  const [captions, setCaptions] = useState<any[]>([]);

  useEffect(() => {
    fetch(staticFile(captionsPath))
      .then((res) => res.json())
      .then((data) => {
        setCaptions(data);
        continueRender(handle);
      })
      .catch((err) => {
        console.error("فشل تحميل ملف الكابشن، سيُكمل الرندرة بدون كابشن:", err);
        continueRender(handle); // نكمل الرندرة بدل ما تعلّق للأبد لو فشل التحميل
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      <Audio src={staticFile(audioPath)} />

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

      <AbsoluteFill
        style={{
          background: "linear-gradient(to top, rgba(0,0,0,0.65) 0%, rgba(0,0,0,0) 30%)",
        }}
      />

      <SyncedCaptions captions={captions} isShort={isShort} />
    </AbsoluteFill>
  );
};
