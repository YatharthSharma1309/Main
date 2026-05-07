import React, { useRef } from 'react';
import '../common/FileInputGroup.css';
import './PdfToImages.css';
import './SinglePdf.css';

const SinglePdf = ({ pdf, loading, error, success, extractionSummary, onPdfChange, onSubmit, canSubmit }) => {
  const inputRef = useRef(null);

  const visionWarning = (() => {
    if (!extractionSummary) return null;
    const m = extractionSummary.match(/vision:(\d+)/);
    const visionCount = m ? parseInt(m[1], 10) : 0;
    if (visionCount === 0) return null;
    const pdfM = extractionSummary.match(/pdf:(\d+)/);
    const pdfCount = pdfM ? parseInt(pdfM[1], 10) : 0;
    const reasonM = extractionSummary.match(/reasons:(.+)/);
    const reason = reasonM ? reasonM[1].trim() : 'no embedded text detected';
    return { visionCount, pdfCount, reason };
  })();

  return (
    <div className="pdf-to-images-form">
      <h2>Single PDF Extract</h2>
      <p className="form-description">
        Upload one PDF containing both questions and answers. Downloads a ZIP with
        <code> extraction_results.xlsx</code> (Q&amp;A data) and one cropped PNG per
        question. Text is extracted directly when possible; Claude vision is used
        only for scanned pages.
      </p>

      <div className="file-input-group">
        <label htmlFor="single-pdf">PDF File *</label>
        <input
          ref={inputRef}
          id="single-pdf"
          type="file"
          accept=".pdf"
          onChange={onPdfChange}
          disabled={loading}
          className="file-input"
        />
        {pdf && (
          <div className="file-info">
            {pdf.name} ({(pdf.size / 1024).toFixed(1)} KB)
          </div>
        )}
      </div>

      {error && <div className="error-message">{error}</div>}

      {success && !visionWarning && (
        <div className="success-message">
          <strong>Done!</strong> ZIP downloaded — contains <code>extraction_results.xlsx</code> and cropped question images.
        </div>
      )}

      {success && visionWarning && (
        <div className="extraction-warning">
          <div className="extraction-warning-title">
            ⚠ Claude Haiku vision was used for {visionWarning.visionCount} question{visionWarning.visionCount !== 1 ? 's' : ''}
          </div>
          <div className="extraction-warning-body">
            {visionWarning.pdfCount} question{visionWarning.pdfCount !== 1 ? 's' : ''} used fast PDF text extraction.
            {visionWarning.visionCount} question{visionWarning.visionCount !== 1 ? 's' : ''} required the vision model because: <em>{visionWarning.reason}</em>.
            Vision rows are highlighted in yellow in the Excel file.
          </div>
        </div>
      )}

      <button
        type="submit"
        disabled={loading || !canSubmit}
        onClick={onSubmit}
        className="submit-button"
      >
        {loading ? 'Processing...' : 'Extract & Download ZIP'}
      </button>
    </div>
  );
};

export default SinglePdf;
