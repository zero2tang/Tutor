import type { Course } from "./types";

export const DEFAULT_COURSE = "mechanics";
export const COURSE_STORAGE_KEY = "stem-tutor-course";
export const SESSION_STORAGE_KEY = "stem-tutor-session";

export function courseTitle(id: string, course?: Course): string {
  return id === "math"
    ? "小学数学与拓展思维"
    : course?.title || "力学与有限元基础";
}

export function courseQuery(path: string, courseId: string): string {
  return `${path}${path.includes("?") ? "&" : "?"}course_id=${encodeURIComponent(courseId)}`;
}
