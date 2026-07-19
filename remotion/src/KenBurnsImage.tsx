import React from "react";
import { Img, interpolate, useCurrentFrame, staticFile } from "remotion";

interface Props {
  src: string;
  durationInFrames: number;
  seed: number;
  isShort?: boolean;
}

/**
 * ملاحظة إصلاح: النسخة السابقة كانت تستقبل startFrame وتطرحه من
 * useCurrentFrame() يدوياً — لكن عند الاستخدام داخل <Sequence from={startFrame}>
 * (وهذا هو الاستخدام الفعلي بـ MainVideo)، Remotion أصلاً يعيد ضبط
 * useCurrentFrame() لتبدأ من صفر عند بداية كل Sequence. يعني كان يصير طرح
 * مضاعف يُنتج localFrame سالب كبير طوال المشهد تقريباً (يفسد نمط الحركة).
 * الحل: الاعتماد على useCurrentFrame() مباشرة كـ"فريم محلي" بدون طرح إضافي.
 */
export const KenBurnsImage: React.FC<Props> = ({
  src, durationInFrames, seed, isShort = true,
}) => {
  const localFrame = useCurrentFrame();
  const progress = Math.max(0, Math.min(1, localFrame / durationInFrames));

  const pattern = seed % 4;
  const zoomStart = 1.0;
  const zoomEnd = 1.15;
  const panLimit = isShort ? 15 : 30;

  let scale = interpolate(progress, [0, 1], [zoomStart, zoomEnd]);
  let translateX = 0;
  let translateY = 0;

  if (pattern === 0) {
    translateX = interpolate(progress, [0, 1], [0, -panLimit]);
  } else if (pattern === 1) {
    scale = interpolate(progress, [0, 1], [zoomEnd, zoomStart]);
    translateY = interpolate(progress, [0, 1], [-20, 0]);
  } else if (pattern === 2) {
    translateX = interpolate(progress, [0, 1], [panLimit, -panLimit]);
    translateY = interpolate(progress, [0, 1], [-15, 15]);
  } else {
    translateX = interpolate(progress, [0, 1], [0, panLimit]);
  }

  return (
    <div style={{ position: "absolute", inset: 0, overflow: "hidden" }}>
      {/* استخدام staticFile لجلب الصورة من مجلد public/assets */}
      <Img
        src={staticFile(src)}
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
