import React, { useEffect, useState } from "react";
import {
  AbsoluteFill, Audio, Sequence, staticFile,
  continueRender, delayRender,
} from "remotion";
import { SceneMedia, MediaItem } from "./SceneMedia";
import { SyncedCaptions } from "./SyncedCaptions";
import { TRANSITION_FRAMES } from "./transitionConfig";

interface RawMediaItem extends MediaItem {
  startFrame: number;
  durationFrames: number;
}

interface Props {
  script: { hook: string; scenes: any[]; closing_cta: string };
  audioPath: string;
  captionsPath: string;
  mediaItems: RawMediaItem[];
  durationSeconds: number;
  width: number;
  height: number;
  fps: number;
}

/**
 * إعادة بناء كاملة لمنطق عرض الوسائط (كانت أهم نقطة بطلب "مونتاج احترافي
 * بدل سلايدشو"):
 *
 * النسخة السابقة: كانت تدور فوق imagePaths بفترة ثابتة (2/2.5 ثانية) بدون
 * أي علاقة بالمحتوى المسموع فعلياً — هذا اللي يعطي إحساس "سلايدشو آلي".
 *
 * النسخة الحالية: تستقبل mediaItems جاهزة من بايثون، كل عنصر مرتبط فعلياً
 * بزمن مشهده الحقيقي بالصوت (voice_and_captions.map_scenes_to_timing)، وقد
 * يكون فيديو (b-roll) أو صورة. كل مشهد يُمدَّد بمقدار TRANSITION_FRAMES
 * على طرفيه (إلا الأول والأخير) لخلق تراكب زمني حقيقي بين Sequence
 * ومجاوره، والتلاشي داخل SceneMedia يحوّل هذا التراكب لانتقال crossfade
 * بدل قطع جاف.
 *
 * إصلاح جوهري محفوظ من النسخة السابقة: تحميل captionsPath فعلياً عبر
 * fetch + delayRender/continueRender بدل مصفوفة فارغة ثابتة بالكود.
 */
export const MainVideo: React.FC<Props> = ({
  audioPath,
  captionsPath,
  mediaItems,
  durationSeconds,
  fps,
  width,
  height,
}) => {
  const isShort = height > width;
  const totalFrames = durationSeconds * fps;

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

  const items = mediaItems && mediaItems.length > 0 ? mediaItems : [];

  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      <Audio src={staticFile(audioPath)} />

      {items.map((item, i) => {
        const isFirst = i === 0;
        const isLast = i === items.length - 1;

        // نمدّد المشهد على طرفيه بمقدار TRANSITION_FRAMES (إلا الأطراف
        // المطلقة لبداية/نهاية الفيديو كامل) عشان يتراكب زمنياً مع جاره
        const renderFrom = isFirst
          ? item.startFrame
          : Math.max(0, item.startFrame - TRANSITION_FRAMES);
        const naturalEnd = item.startFrame + item.durationFrames;
        const renderEnd = isLast
          ? Math.min(totalFrames, naturalEnd)
          : Math.min(totalFrames, naturalEnd + TRANSITION_FRAMES);
        const renderDuration = Math.max(1, renderEnd - renderFrom);

        const fadeInFrames = isFirst ? 0 : item.startFrame - renderFrom;
        const fadeOutFrames = isLast ? 0 : renderEnd - naturalEnd;

        return (
          <Sequence key={i} from={renderFrom} durationInFrames={renderDuration}>
            <SceneMedia
              item={{ type: item.type, src: item.src }}
              seed={i}
              isShort={isShort}
              renderDuration={renderDuration}
              fadeInFrames={fadeInFrames}
              fadeOutFrames={fadeOutFrames}
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
