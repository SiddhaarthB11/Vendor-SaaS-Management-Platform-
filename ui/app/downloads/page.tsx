import Link from "next/link";
import "./downloads.css";

export default function DownloadsPage() {
  return (
    <div className="downloads-page">
      <header className="downloads-header">
        <h1>Downloads</h1>
        <p>Download blank templates or view examples for bulk data uploads.</p>
      </header>

      <section className="downloads-section">
        <h2>Subscriptions</h2>
        <p>Use these templates to upload multiple subscriptions at once.</p>
        <div className="downloads-grid">
          <div className="download-card">
            <h3>Blank Template</h3>
            <p>A blank CSV file with the required headers for bulk upload.</p>
            <a href="/templates/subscriptions_template.csv" download className="k-btn">
              Download Template
            </a>
          </div>
          <div className="download-card">
            <h3>Filled Example</h3>
            <p>A sample CSV file demonstrating the correct data format.</p>
            <a href="/templates/subscriptions_sample.csv" download className="k-btn">
              Download Example
            </a>
          </div>
        </div>
      </section>

      <section className="downloads-section">
        <h2>Licences</h2>
        <p>Use these templates to upload multiple licences and assignments at once.</p>
        <div className="downloads-grid">
          <div className="download-card">
            <h3>Blank Template</h3>
            <p>A blank CSV file with the required headers for bulk upload.</p>
            <a href="/templates/licences_template.csv" download className="k-btn">
              Download Template
            </a>
          </div>
          <div className="download-card">
            <h3>Filled Example</h3>
            <p>A sample CSV file demonstrating the correct data format.</p>
            <a href="/templates/licences_sample.csv" download className="k-btn">
              Download Example
            </a>
          </div>
        </div>
      </section>
    </div>
  );
}
