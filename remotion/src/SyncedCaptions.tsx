import React from "react";
import { useCurrentFrame, useVideoConfig, interpolate, spring } from "remotion";

interface WordEvent {
  word: string;
  start_ms: number;
  duration_ms: number;
}

interface Props {
  captions: WordEvent[];
  wordsPerGroup?: number;
  isShort?: boolean;
}

export const SyncedCaptions: React.FC<Props> = ({ captions, wordsPerGroup = 4, isShort = true }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const currentMs = (frame / fps) * 1000;

  // التحقق من وجود البيانات لتجنب أي خطأ برمجياً
  if (!captions || !Array.isArray(captions) || captions.length === 0) {
    return null;
  }

  const activeIndex = captions.findIndex(
    (c) => currentMs >= c.start_ms && currentMs < c.start_ms + c.duration_ms + 150
  );
  if (activeIndex === -1) return null;

  const groupStart = Math.floor(activeIndex / wordsPerGroup) * wordsPerGroup;
  const groupWords = captions.slice(groupStart, groupStart + wordsPerGroup);

  // حساب فريم بداية الكلمة النشطة لعمل تأثير spring
  const activeWordStartFrame = Math.round((captions[activeIndex].start_ms / 1000) * fps);

  const bottomSafe = isShort ? "26%" : "12%";
  const rightSafe = isShort ? "14%" : "6%";
  const leftSafe = isShort ? "4%" : "6%";

  return (
    <div style={{
        position: "absolute", bottom: bottomSafe, left: leftSafe, right: rightSafe,
        display: "flex", flexWrap: "wrap", justifyContent: "center", gap: "10px",
        fontFamily: "Montserrat, sans-serif", fontSize: isShort ? 52 : 58,
        fontWeight: 800, textTransform: "uppercase", textAlign: "center",
      }}>
      {groupWords.map((w, i) => {
        const wordGlobalIndex = groupStart + i;
        const isActive = wordGlobalIndex === activeIndex;

        // Spring animation: الكلمة تدخل بنبضة (pop-in) ثم تستقر
        const scaleSpring = isActive
          ? spring({
              frame: frame - activeWordStartFrame,
              fps,
              config: { damping: 12, stiffness: 200, mass: 0.6 },
            })
          : 1;

        // حجم أكبر قليلاً للكلمة المنطوقة + glow
        const activeScale = isActive ? 1.0 + scaleSpring * 0.12 : 1.0;

        return (
          <span key={`${groupStart}-${i}`} style={{
              color: isActive ? "#00E5FF" : "#FFFFFF",
              textShadow: isActive
                ? "0px 0px 20px rgba(0,229,255,0.6), 0px 4px 10px rgba(0,0,0,0.85)"
                : "0px 4px 10px rgba(0,0,0,0.85)",
              transform: `scale(${activeScale})`,
              display: "inline-block",
            }}>
            {w.word}
          </span>
        );
      })}
    </div>
  );
};
