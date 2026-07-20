import React from "react";
import { Composition, getInputProps } from "remotion";
import { MainVideo } from "./MainVideo";

// نقرأ props الفعلية وقت الرندرة عبر --props من daily_pipeline.py،
// هذه فقط قيم افتراضية للمعاينة المحلية بـ `npm run preview`.
const defaultProps = {
  script: { hook: "", scenes: [], closing_cta: "" },
  audioPath: "",
  captions: [], // التعديل هنا: تم تغيير captionsPath إلى مصفوفة فارغة لتمرير البيانات مباشرة
  // mediaItems يحل محل imagePaths القديم: كل عنصر يمثل مشهد واحد بزمنه
  // الحقيقي بالصوت (startFrame/durationFrames) بدل صور تدور بفترة ثابتة
  mediaItems: [],
  durationSeconds: 300,
  width: 1920,
  height: 1080,
  fps: 30,
};

// نحسب المدة الفعلية من الـ props الممررة وقت الرندرة (--props)
// بدل القيمة الثابتة 55*30 التي كانت تسبب شاشة سوداء في نهاية الفيديو
const inputProps = getInputProps() as Record<string, unknown>;
const actualFps = (inputProps?.fps as number) || 30;
const actualShortDuration = (inputProps?.durationSeconds as number) || 55;
const shortFrames = Math.round(actualShortDuration * actualFps);

export const RemotionRoot: React.FC = () => {
  return (
    <>
      {/* ⚠️ غير نشط حالياً بالجدولة (راجع long_video_pipeline_FUTURE.yml) —
          جاهز للمرحلة القادمة بعد نتائج مستقرة من الشورتس.
          الفيديو الطويل: 1920x1080 — دقة 1080p كحد أدنى مضمون بالكود مباشرة */}
      <Composition
        id="LongVideo"
        component={MainVideo}
        durationInFrames={5 * 60 * 30} // 5 دقائق افتراضياً بـ 30fps، القيمة الفعلية تُحسب وقت الرندرة
        fps={30}
        width={1920}
        height={1080}
        defaultProps={defaultProps}
      />

      {/* ✅ المسار النشط حالياً (shorts_pipeline.yml) — عمودي 1080x1920،
          المدة تُحسب ديناميكياً من مدة الصوت الفعلية بدل 55 ثانية ثابتة */}
      <Composition
        id="ShortVideo"
        component={MainVideo}
        durationInFrames={shortFrames}
        fps={actualFps}
        width={1080}
        height={1920}
        defaultProps={{ ...defaultProps, width: 1080, height: 1920 }}
      />
    </>
  );
};
