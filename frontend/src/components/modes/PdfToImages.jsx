import React, { useRef } from 'react';
import '../common/FileInputGroup.css';
import './PdfToImages.css';

const MODEL_OPTIONS = [
  { value: 'sonnet',    label: 'Claude Sonnet (recommended)' },
  { value: 'haiku',     label: 'Claude Haiku (faster)' },
  { value: 'gpt-4o',   label: 'GPT-4o' },
  { value: 'gpt-4o-mini', label: 'GPT-4o Mini' },
  { value: 'qwen2.5vl:7b', label: 'Ollama (local)' },
];

const PdfToImages = ({
  questionsPdf,
  answersPdf,
  model,
  loading,
  error,
  success,
  result,
  onPdfChange,
  onModelChange,
  onSubmit,
  canSubmit,
}) => {
  const questionsInputRef = useRef(null);
  const answersInputRef = useRef(null);

  return (
    <div className="pdf-to-images-form">
      <h2>Convert PDF to Images</h2>
      <p className="form-description">
        Renders every page as a PNG (saved to <code>pages/</code>) and crops
        each detected question into its own PNG (saved to <code>questions/</code>).
      </p>

      <div className="file-input-group">
        <label htmlFor="q-pdf">Questions PDF *</label>
        <input
          ref={questionsInputRef}
          id="q-pdf"
          type="file"
          accept=".pdf"
          onChange={(e) => onPdfChange(e, 'Questions PDF', 'questions')}
          disabled={loading}
          className="file-input"
        />
        {questionsPdf && (
          <div className="file-info">
            {questionsPdf.name} ({(questionsPdf.size / 1024).toFixed(1)} KB)
          </div>
        )}
      </div>

      <div className="file-input-group">
        <label htmlFor="a-pdf">Answers PDF <span style={{fontWeight:'normal',color:'#888'}}>(optional — uses questions PDF if omitted)</span></label>
        <input
          ref={answersInputRef}
          id="a-pdf"
          type="file"
          accept=".pdf"
          onChange={(e) => onPdfChange(e, 'Answers PDF', 'answers')}
          disabled={loading}
          className="file-input"
        />
        {answersPdf && (
          <div className="file-info">
            {answersPdf.name} ({(answersPdf.size / 1024).toFixed(1)} KB)
          </div>
        )}
      </div>

      <div className="file-input-group">
        <label htmlFor="pti-model">Vision Model</label>
        <select
          id="pti-model"
          value={model}
          onChange={(e) => onModelChange(e.target.value)}
          disabled={loading}
          className="model-select"
        >
          {MODEL_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
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
        {loading ? 'Processing...' : 'Convert to Images'}
      </button>
    </div>
  );
};

export default PdfToImages;
