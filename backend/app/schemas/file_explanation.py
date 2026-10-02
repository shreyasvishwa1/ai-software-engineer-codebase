from pydantic import BaseModel

# An AI-generated explanation of one repository file. Deliberately excluded: file ids,
# sha256 checksums, file contents, filesystem paths and any provider request details.


class FileExplanationResponse(BaseModel):
    path: str  # POSIX path relative to the repository root
    explanation: str  # plain text from the model
    model: str  # the model that produced the explanation
    input_tokens: int | None
    output_tokens: int | None
