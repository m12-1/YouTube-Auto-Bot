import React from "react";
import { useCurrentFrame, useVideoConfig, interpolate, spring, Easing } from "remotion";

/**
 * تأثير بصري "الخطاف" (Hook) — يظهر في أول 2-3 ثوانٍ فقط من الفيديو.
 * يعطي إحساساً بالإثارة والفضول لجذب المشاهد فوراً قبل التبديل.
 * 
 * يتكون من:
 * 1. ومضة بيضاء سريعة (Flash) عند البداية
 * 2. تكبير سريع للمشهد الأول (Zoom punch)
 * 3. ظل متدرج أقوى في الأسفل لبروز النص
 */

interface Props {
  isShort?: boolean;
}

export const HookEffect: React.FC<Props> = ({ isShort = true }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // ===== 1. ومضة بيضاء سريعة في أول 4 فريمات (~0.13 ثانية) =====
  const flashOpacity = interpolate(frame, [0, 3, 6], [0.85, 0.4, 0], {
    extrapolateRight: "clamp",
    extrapolateLeft: "clamp",
  });

  // ===== 2. Vignette (ظل حواف) يتلاشى خلال الثانيتين الأولى =====
  const vignetteOpacity = interpolate(frame, [0, fps * 2], [0.5, 0], {
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.ease),
  });

  // ===== 3. خط سفلي متوهج يختفي بعد 3 ثوانٍ =====
  const glowOpacity = interpolate(frame, [fps * 0.5, fps * 1.5, fps * 3], [0, 0.7, 0], {
    extrapolateRight: "clamp",
    extrapolateLeft: "clamp",
  });

  return (
    <>
      {/* ومضة بيضاء افتتاحية */}
      {frame < 8 && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            backgroundColor: "#FFFFFF",
            opacity: flashOpacity,
            zIndex: 10,
          }}
        />
      )}

      {/* Vignette حول الحواف */}
      {vignetteOpacity > 0.01 && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            background:
              "radial-gradient(ellipse at center, transparent 50%, rgba(0,0,0,0.8) 100%)",
            opacity: vignetteOpacity,
            zIndex: 5,
          }}
        />
      )}

      {/* وهج سفلي يلفت الانتباه لمنطقة الكابشن */}
      {glowOpacity > 0.01 && (
        <div
          style={{
            position: "absolute",
            bottom: 0,
            left: 0,
            right: 0,
            height: isShort ? "35%" : "25%",
            background:
              "linear-gradient(to top, rgba(0,229,255,0.15) 0%, transparent 100%)",
            opacity: glowOpacity,
            zIndex: 5,
          }}
        />
      )}
    </>
  );
};
