import { PdfAnalyzer } from "../components/pdf-analyzer";

export default function Home() {
  return (
    <main className="app-shell">
      <section className="workspace">
        <div className="heading">
          <p>JUB/KOOP interno</p>
          <h1>PDF pipeline</h1>
        </div>
        <PdfAnalyzer />
      </section>
    </main>
  );
}

