import React, { useRef } from 'react';
import '../common/FileInputGroup.css';
import './MathpixExtractor.css';

const MODEL_OPTIONS = [
  { value: 'text',  label: 'Text (plain output)' },
  { value: 'latex', label: 'LaTeX (math equations)' },
  { value: 'full',  label: 'Full (text + LaTeX)' },
];

const MathpixExtractor = ({
  questionsPdf,
  answersPdf,
  model,
  loading,
  error,
  success,
  onPdfChange,
  onModelChange,
  onSubmit,
  canSubmit,
}) => {
  const questionsInputRef = useRef(null);
  const answersInputRef   = useRef(null);

  return (
    <div className="mathpix-extractor-form">
      <h2>Extract Q&amp;A with Mathpix</h2>

      <div className="mathpix-info">
        <strong>Mathpix</strong> is a cloud OCR API specialised in scientific
        documents — it accurately captures LaTeX math, chemical formulae, and
        complex diagrams that plain-text extractors miss.
        <br /><br />
        <strong>Requires</strong> <code>MATHPIX_APP_ID</code> and{' '}
        <code>MATHPIX_APP_KEY</code> environment variables on the server.
      </div>

      <div className="file-input-group">
        <label htmlFor="mathpix-questions">Questions PDF *</label>
        <input
          ref={questionsInputRef}
          id="mathpix-questions"
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
        <label htmlFor="mathpix-answers">Answers PDF <span style={{fontWeight:'normal',color:'#888'}}>(optional — uses questions PDF if omitted)</span></label>
        <input
          ref={answersInputRef}
          id="mathpix-answers"
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
        <label htmlFor="mathpix-model">Output Format</label>
        <select
          id="mathpix-model"
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
          Mathpix extraction complete. Excel downloaded.
        </div>
      )}

      <button
        type="submit"
        disabled={loading || !canSubmit}
        onClick={onSubmit}
        className="submit-button mathpix"
      >
        {loading ? 'Running Mathpix OCR…' : 'Extract with Mathpix & Download'}
      </button>
    </div>
  );
};

export default MathpixExtractor;
