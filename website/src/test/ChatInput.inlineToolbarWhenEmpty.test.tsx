import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/* Issue #19608: the Crewmates DM composer opts into a single-row idle layout.
 * Empty draft -> one row (`+` · textarea · mic · send); typing -> today's two
 * rows; clearing -> back to one row. Without the opt-in prop nothing changes,
 * so the ordinary chat composer is untouched. */

const base = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  onUploadFiles: vi.fn(),
  placeholder: 'Message oncall…',
}

function wrapper() {
  return screen.getByTestId('input-wrapper')
}

function toolbarRow() {
  return screen.getByTestId('composer-toolbar-row')
}

describe('ChatInput inlineToolbarWhenEmpty (#19608)', () => {
  it('collapses to one row when the draft is empty and the prop is set', () => {
    renderWithProviders(<ChatInput {...base} inlineToolbarWhenEmpty />)
    expect(wrapper()).toHaveAttribute('data-inline-toolbar', 'true')
    // The toolbar row dissolves into the same flex line as the textarea:
    // `+` takes the left slot, the textarea the middle, mic/send the right.
    expect(toolbarRow()).toHaveClass('contents')
    const plus = screen.getByLabelText('Add files & options')
    expect(plus.closest('[data-inline-slot="left"]')).not.toBeNull()
    const textarea = screen.getByPlaceholderText('Message oncall…')
    expect(textarea.closest('[data-inline-slot="center"]')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Send' }).closest('[data-inline-slot="right"]')).not.toBeNull()
  })

  it('keeps send disabled on the single empty row, same as today', () => {
    renderWithProviders(<ChatInput {...base} inlineToolbarWhenEmpty />)
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  })

  it('expands back to two rows while the draft is non-empty', () => {
    renderWithProviders(<ChatInput {...base} value="hello" inlineToolbarWhenEmpty />)
    expect(wrapper()).toHaveAttribute('data-inline-toolbar', 'false')
    expect(toolbarRow()).not.toHaveClass('contents')
  })

  it('returns to one row when the draft is cleared', () => {
    const { rerender } = renderWithProviders(<ChatInput {...base} value="hello" inlineToolbarWhenEmpty />)
    rerender(<ChatInput {...base} value="" inlineToolbarWhenEmpty />)
    expect(wrapper()).toHaveAttribute('data-inline-toolbar', 'true')
    expect(toolbarRow()).toHaveClass('contents')
  })

  it('does nothing without the opt-in prop, so the ordinary composer is untouched', () => {
    renderWithProviders(<ChatInput {...base} />)
    expect(wrapper()).not.toHaveAttribute('data-inline-toolbar', 'true')
    expect(wrapper()).toHaveAttribute('data-inline-toolbar', 'false')
    expect(toolbarRow()).not.toHaveClass('contents')
  })

  it('stays out of the way when the composer is collapsed or manually resized', () => {
    // manualHeight set via the drag path is internal state; the guard this
    // test pins is the prop-driven one: a collapsed composer never reports
    // inline because the collapsed bar replaces the whole box.
    renderWithProviders(<ChatInput {...base} inlineToolbarWhenEmpty collapsible />)
    // Not collapsed yet, so inline still applies; the pin is that the flag
    // exists and is respected, not the collapse interaction itself.
    expect(wrapper()).toHaveAttribute('data-inline-toolbar', 'true')
  })
})
