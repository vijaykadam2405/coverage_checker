import { Radar } from "lucide-react";

export function Empty({
  title = "Your website analysis starts here",
  children,
}) {
  return (
    <div className="empty">
      <Radar size={36} />
      <h3>{title}</h3>
      <p>
        {children ||
          "Enter a website, choose what to capture, and discover its pages. Corpus checks start only when you ask."}
      </p>
    </div>
  );
}
