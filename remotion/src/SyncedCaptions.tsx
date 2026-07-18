import React from "react";
import { useCurrentFrame, useVideoConfig, interpolate } from "remotion";

interface WordEvent {
  word: string;
  start_ms: number;
  duration_ms: number;
}

interface Props {
  captions: WordEvent[];
  wordsPerGroup?: number; // كم كلمة تظهر سوية بنفس اللحظة (2-3 أفضل من جملة كاملة)
  isShort?: boolean; // يفعّل هوامش أمان يوتيوب شورتس (أزرار جانبية + شريط سفلي)
}

/**
 * كابشن ينبض مع الصوت كلمة-بكلمة (استناداً لتوقيت edge-tts WordBoundary)،
 * بدل جملة كاملة ثابتة — هذا أهم عنصر بمنع الملل البصري أثناء القراءة.
 *
 * هوامش الأمان (Safe Zone) للشورت مبنية على واجهة يوتيوب الفعلية:
 * - يمين 14%: مكان أزرار like/comment/share/subscribe
 * - أسفل 26%: مكان عنوان الفيديو/اسم القناة/الصوت المستخدم
 * - يسار 4%: مسافة توازن بصري بسيطة (لا توجد عناصر واجهة هناك لكن تحسّن القراءة)
 * - أعلى 8%: احتياط لمعاينات الواجهة المختلفة
 */
export const SyncedCaptions: React.FC<Props> = ({ captions, wordsPerGroup = 3, isShort = true }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const currentMs = (frame / fps) * 1000;

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

  // هوامش الشورت الفعلية (يمين أوسع بسبب أزرار الواجهة، أسفل أوسع بسبب
  // عنوان/اسم القناة) — للفيديو الأفقي الطويل نستخدم هامش موحّد أبسط
  const bottomSafe = isShort ? "26%" : "12%";
  const rightSafe = isShort ? "14%" : "6%";
  const leftSafe = isShort ? "4%" : "6%";

  return (
    <div
      style={{
        position: "absolute",
        bottom: bottomSafe,
        left: leftSafe,
        right: rightSafe,
        display: "flex",
        flexWrap: "wrap",
        justifyContent: "center",
        gap: "12px",
        fontFamily: "Arial Black, sans-serif",
        fontSize: isShort ? 58 : 64,
        fontWeight: 900,
        textTransform: "uppercase",
        textAlign: "center",
      }}
    >
      {groupWords.map((w, i) => {
        const isActive = captions.indexOf(w) === activeIndex;
        return (
          <span
            key={i}
            style={{
              color: isActive ? "#FFD400" : "#FFFFFF",
              WebkitTextStroke: "3px black",
              transform: isActive ? `scale(${pulse})` : "scale(1)",
              display: "inline-block",
            }}
          >
            {w.word}
          </span>
        );
      })}
    </div>
  );
};
