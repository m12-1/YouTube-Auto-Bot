import React from "react";
import { Composition } from "remotion";
import { MainVideo } from "./MainVideo";

// نقرأ props الفعلية وقت الرندرة عبر --props من daily_pipeline.py،
// هذه فقط قيم افتراضية للمعاينة المحلية بـ `npm run preview`.
const defaultProps = {
  script: { hook: "", scenes: [], closing_cta: "" },
  audioPath: "",
  captionsPath: "",
  imagePaths: [],
  durationSeconds: 300,
  width: 1920,
  height: 1080,
  fps: 30,
};

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
          العرض 1080 هو الحد الأدنى المضمون للدقة */}
      <Composition
        id="ShortVideo"
        component={MainVideo}
        durationInFrames={55 * 30}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{ ...defaultProps, width: 1080, height: 1920 }}
      />
    </>
  );
};
