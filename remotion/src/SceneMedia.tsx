import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { KenBurnsImage } from "./KenBurnsImage";
import { SceneVideo } from "./SceneVideo";

export interface MediaItem {
  type: "video" | "image";
  src: string;
}

interface Props {
  item: MediaItem;
  seed: number;
  isShort: boolean;
  renderDuration: number; // مدة عرض الـ Sequence الفعلية (بعد إضافة هامش التلاشي)
  fadeInFrames: number; // 0 لأول مشهد (يبدأ ظاهر بالكامل بدون تلاشي دخول)
  fadeOutFrames: number; // 0 لآخر مشهد (ينتهي فجأة مع نهاية الفيديو، وهذا مقصود)
}

/**
 * يعرض مشهد واحد (فيديو أو صورة) مع تلاشي دخول/خروج بدل القطع الجاف —
 * هذا أهم فرق بصري بين "سلايدشو آلي" و"مونتاج يشبه شغل بشري".
 *
 * useCurrentFrame() هنا هو الفريم *المحلي* لأن هذا المكوّن دايماً يُستخدم
 * داخل <Sequence> بـ MainVideo، اللي يعيد ضبط العداد تلقائياً من صفر —
 * فلا حاجة لأي طرح يدوي لأي startFrame هنا.
 */
export const SceneMedia: React.FC<Props> = ({
  item, seed, isShort, renderDuration, fadeInFrames, fadeOutFrames,
}) => {
  const localFrame = useCurrentFrame();

  const fadeIn = fadeInFrames > 0
    ? interpolate(localFrame, [0, fadeInFrames], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
    : 1;
  const fadeOut = fadeOutFrames > 0
    ? interpolate(
        localFrame,
        [renderDuration - fadeOutFrames, renderDuration],
        [1, 0],
        { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
      )
    : 1;
  const opacity = Math.min(fadeIn, fadeOut);

  return (
    <div style={{ position: "absolute", inset: 0, opacity }}>
      {item.type === "video" ? (
        <SceneVideo src={item.src} durationInFrames={renderDuration} seed={seed} />
      ) : (
        <KenBurnsImage src={item.src} durationInFrames={renderDuration} seed={seed} isShort={isShort} />
      )}
    </div>
  );
};
