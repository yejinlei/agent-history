import { useCallback, useRef } from "react";

interface Props {
  /** 拖拽轴：x = 竖条改宽度；y = 横条改高度 */
  axis: "x" | "y";
  /** 从按下点累计的位移（px） */
  onDrag: (delta: number) => void;
  /** 按下时回调（记录拖拽基准值） */
  onStart?: () => void;
  /** 松手回调 */
  onRelease?: () => void;
  title?: string;
}

/** 通用可拖拽分隔条：pointer 事件 + 全局光标/禁选，不依赖外部库 */
export default function SplitBar({ axis, onDrag, onStart, onRelease, title }: Props) {
  const start = useRef(0);
  const dragging = useRef(false);

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      dragging.current = true;
      start.current = axis === "x" ? e.clientX : e.clientY;
      onStart?.();
      document.body.classList.add(
        axis === "x" ? "splitting-x" : "splitting-y");
      (e.target as HTMLElement).setPointerCapture?.(e.pointerId);

      const move = (ev: PointerEvent) => {
        if (!dragging.current) return;
        const p = axis === "x" ? ev.clientX : ev.clientY;
        onDrag(p - start.current);
      };
      const up = () => {
        dragging.current = false;
        document.body.classList.remove("splitting-x", "splitting-y");
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        onRelease?.();
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    },
    [axis, onDrag, onStart, onRelease],
  );

  return (
    <div
      className={`splitbar ${axis === "x" ? "sb-x" : "sb-y"}`}
      title={title ?? "拖拽调整大小"}
      onPointerDown={onPointerDown}
    >
      <span className="sb-grip" />
    </div>
  );
}
