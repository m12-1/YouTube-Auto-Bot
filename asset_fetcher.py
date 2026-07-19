import React from "react";
import { OffthreadVideo, Loop, interpolate, useCurrentFrame, staticFile } from "remotion";

interface Props {
  src: string;
  durationInFrames: number;
  seed: number;
}

/**
 * يعرض مقطع فيديو (b-roll من Pixabay) بدل صورة ثابتة. مقاطع Pixabay مدتها
 * غير معروفة مسبقاً وقد تكون أقصر من مدة المشهد المطلوبة، لذا نغلّفه بـ
 * <Loop> (ميزة رسمية بـ Remotion) بدل الاعتماد على مدة معروفة مسبقاً — لو
 * كان المقطع أطول من اللازم، Loop تكتفي بعرضه مرة وحيدة بالمدة المطلوبة.
 *
 * زوم بسيط جداً (1.0 → 1.06) بدل ثبات كامل: يعطي إحساس "كاميرا حيّة" حتى
 * فوق الفيديو نفسه، بنفس روح KenBurnsImage لكن أخف بكثير لأن الفيديو أصلاً
 * فيه حركة.
 */
export const SceneVideo: React.FC<Props> = ({ src, durationInFrames, seed }) => {
  const frame = useCurrentFrame();
  const progress = Math.max(0, Math.min(1, frame / durationInFrames));
  const zoomIn = seed % 2 === 0;
  const scale = interpolate(progress, [0, 1], zoomIn ? [1.0, 1.06] : [1.06, 1.0]);

  // مدة تقديرية للتكرار الداخلي (10 ثوان بـ30fps) — Loop تقصّها تلقائياً
  // لمدة المشهد الفعلية، القيمة هنا فقط سقف أعلى آمن
  const LOOP_CHUNK = 300;

  return (
    <div style={{ position: "absolute", inset: 0, overflow: "hidden" }}>
      <div style={{ width: "100%", height: "100%", transform: `scale(${scale})` }}>
        <Loop durationInFrames={LOOP_CHUNK}>
          <OffthreadVideo
            src={staticFile(src)}
            muted
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        </Loop>
      </div>
    </div>
  );
};
