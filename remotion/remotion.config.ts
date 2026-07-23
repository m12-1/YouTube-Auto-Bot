import { Config } from "@remotion/cli/config";

// دقة 1080p كحد أدنى مضمون (طلب المستخدم صراحة) — نضبطها هنا كإعداد افتراضي
// عام على مستوى المشروع، والـ Composition نفسه يحدد نفس الأبعاد بالضبط.
Config.setVideoImageFormat("jpeg");
Config.setOverwriteOutput(true);
Config.setCrf(18); // جودة عالية (رقم أقل = جودة أعلى بـ ffmpeg CRF scale)
Config.setCodec("h264");
Config.setPixelFormat("yuv420p"); // توافق كامل مع مشغل يوتيوب
Config.setConcurrency(1); // GitHub Actions runner محدود الموارد — رندرة تسلسلية أكثر استقراراً
