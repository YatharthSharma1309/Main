import React, { useRef } from 'react';
import '../common/FileInputGroup.css';
import './PdfToImages.css';

const SinglePdf = ({ pdf, loading, error, success, onPdfChange, onSubmit, canSubmit }) => {
  const inputRef = useRef(null);

  return (
    <div className="pdf-to-images-form">
      <h2>Single PDF Extract</h2>
      <p className="form-description">
        Upload one PDF containing both questions and answers. Each question will be
        cropped to an image and transcribed using Claude (Sonnet), and the extracted
        Q&amp;A will be returned as an Excel file.
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

      {success && (
        <div className="success-message">
          <strong>Done!</strong> Your Excel file has been downloaded.
        </div>
      )}

      <button
        type="submit"
        disabled={loading || !canSubmit}
        onClick={onSubmit}
        className="submit-button"
      >
        {loading ? 'Processing...' : 'Extract Q&A'}
      </button>
    </div>
  );
};

export default SinglePdf;
