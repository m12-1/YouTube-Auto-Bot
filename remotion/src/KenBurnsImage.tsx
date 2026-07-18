import React from "react";
import { Img, interpolate, useCurrentFrame } from "remotion";

interface Props {
  src: string;
  startFrame: number;
  durationInFrames: number;
  seed: number; // يحدد اتجاه/شدة الحركة بشكل مختلف لكل صورة (يمنع التكرار المملّ)
  isShort?: boolean; // يقلل شدة الحركة الأفقية حتى لا يُدفع محور الصورة تحت أزرار يمين الشاشة
}

/**
 * تأثير Ken Burns: zoom + pan متغير حسب seed كل صورة، بدل حركة ثابتة واحدة
 * تتكرر بكل الفيديو (وهذا بالضبط ما يجعل الفيديو "لا يمل عند مشاهدته").
 */
export const KenBurnsImage: React.FC<Props> = ({
  src, startFrame, durationInFrames, seed, isShort = true,
}) => {
  const frame = useCurrentFrame();
  const localFrame = frame - startFrame;
  const progress = Math.max(0, Math.min(1, localFrame / durationInFrames));

  // أربعة أنماط حركة تتبدل حسب seed % 4 — يكسر رتابة "نفس الزوم بكل مرة"
  const pattern = seed % 4;
  const zoomStart = 1.0;
  const zoomEnd = 1.15;
  // بالشورت نقلل شدة الـ pan الأفقي حتى لا يُدفع الموضوع الرئيسي بالصورة
  // تحت أزرار الواجهة يمين الشاشة (14% محجوزة كـ safe zone)
  const panLimit = isShort ? 15 : 30;

  let scale = interpolate(progress, [0, 1], [zoomStart, zoomEnd]);
  let translateX = 0;
  let translateY = 0;

  if (pattern === 0) {
    // zoom in مع pan يمين خفيف
    translateX = interpolate(progress, [0, 1], [0, -panLimit]);
  } else if (pattern === 1) {
    // zoom out
    scale = interpolate(progress, [0, 1], [zoomEnd, zoomStart]);
    translateY = interpolate(progress, [0, 1], [-20, 0]);
  } else if (pattern === 2) {
    // pan قطري (بشدة مخفضة بالشورت)
    translateX = interpolate(progress, [0, 1], [panLimit, -panLimit]);
    translateY = interpolate(progress, [0, 1], [-15, 15]);
  } else {
    // zoom in مع pan يسار
    translateX = interpolate(progress, [0, 1], [0, panLimit]);
  }

  return (
    <div style={{ position: "absolute", inset: 0, overflow: "hidden" }}>
      <Img
        src={src}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          transform: `scale(${scale}) translate(${translateX}px, ${translateY}px)`,
        }}
      />
    </div>
  );
};
