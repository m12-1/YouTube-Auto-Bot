import React from "react";
import { useCurrentFrame, useVideoConfig, interpolate } from "remotion";

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

  const pulse = interpolate(
    currentMs - captions[activeIndex].start_ms,
    [0, captions[activeIndex].duration_ms],
    [1.08, 1],
    { extrapolateRight: "clamp" }
  );

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
        const isActive = captions.indexOf(w) === activeIndex;
        return (
          <span key={i} style={{
              color: isActive ? "#00E5FF" : "#FFFFFF",
              textShadow: "0px 4px 10px rgba(0,0,0,0.85)",
              transform: isActive ? `scale(${pulse})` : "scale(1)",
              display: "inline-block",
              transition: "color 0.1s ease-in-out",
            }}>
            {w.word}
          </span>
        );
      })}
    </div>
  );
};
