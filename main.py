# smart_reschedule.py
import os
import json
import re
import yaml
from datetime import datetime, timedelta
from todoist_api_python.api import TodoistAPI


class TodoistMetadataManager:
    def __init__(self, api_token):
        self.api = TodoistAPI(api_token)

    def find_metadata_comment(self, task_id):
        """Find and return the metadata comment for a task"""
        try:
            comments_iter = self.api.get_comments(task_id=task_id)
            # Flatten the iterator of lists
            all_comments = []
            for comment_list in comments_iter:
                all_comments.extend(comment_list)
            
            for comment in all_comments:
                if comment.content.startswith("# METADATA"):
                    return comment
        except:
            pass
        return None

    def get_metadata(self, task):
        """Extract metadata from task comments"""
        comment = self.find_metadata_comment(task.id)
        if comment:
            try:
                # Extract YAML from comment
                yaml_content = comment.content.replace("# METADATA\n", "")
                return yaml.safe_load(yaml_content)
            except:
                pass
        return {"failures": 0, "successes": 0, "created": datetime.now().isoformat()}

    def update_metadata(self, task_id, updates):
        """Update task metadata via comments"""
        task = self.api.get_task(task_id)
        metadata = self.get_metadata(task)
        metadata.update(updates)

        # Check if metadata comment already exists
        try:
            existing_comment = self.find_metadata_comment(task_id)
            
            # Create YAML content
            yaml_content = f"# METADATA\n{yaml.dump(metadata, default_flow_style=False)}"
            
            if existing_comment:
                # Update existing comment
                self.api.update_comment(comment_id=existing_comment.id, content=yaml_content)
            else:
                # Create new comment
                self.api.add_comment(task_id=task_id, content=yaml_content)
        except Exception as e:
            print(f"Error updating metadata: {e}")
        
        return metadata

    def filter_tasks_flattened(self, query):
        """Filter tasks and return a flattened list"""
        tasks = self.api.filter_tasks(query=query)
        # Convert iterator to list and flatten if needed
        task_list = list(tasks)
        if task_list and hasattr(task_list[0], '__iter__') and not hasattr(task_list[0], 'id'):
            # Flatten iterator of lists
            flattened = []
            for item in task_list:
                flattened.extend(item)
            return flattened
        return task_list

    def mark_failure(self, task):
        """Mark a task as failed and update metadata"""
        metadata = self.get_metadata(task)
        failures = metadata.get("failures", 0) + 1

        self.update_metadata(task.id, {
            "failures": failures,
            "last_failed": datetime.now().isoformat()
        })
        
        return failures

    def calculate_delay_hours(self, failures):
        """Calculate delay hours based on failure count"""
        return min(24 * (2 ** (failures - 1)), 168)  # Cap at 1 week

    def get_success_ratio(self, task):
        """Calculate success to failure ratio for prioritization"""
        metadata = self.get_metadata(task)
        successes = metadata.get("successes", 0)
        failures = metadata.get("failures", 0)
        
        # Add small epsilon to avoid division by zero
        return (successes + 1) / (failures + 1)

    def reschedule_task(self, task, new_due_date, failures):
        """Reschedule a single task to a specific date"""
        prefix = "🔄" if failures >= 3 else ""
        suffix = f" (Failed {failures}x)" if failures > 1 else ""

        self.api.update_task(
            task_id=task.id,
            due_date=new_due_date.date() if hasattr(new_due_date, 'date') else new_due_date,
            content=f"{prefix}{task.content.replace('🔄', '').split(' (Failed')[0]}{suffix}"
        )

        print(f"Rescheduled: {task.content} → {new_due_date.strftime('%Y-%m-%d')} (failure #{failures})")

    def batch_reschedule_overdue(self, tasks):
        """Reschedule overdue tasks with one task per day, prioritized by success ratio"""
        if not tasks:
            return
        
        # Separate high priority tasks (priority >= 3) from regular tasks
        high_priority_tasks = [task for task in tasks if task.priority >= 3]
        regular_tasks = [task for task in tasks if task.priority < 3]
        
        # Process high priority tasks - always reschedule to today
        for task in high_priority_tasks:
            failures = self.mark_failure(task)
            today = datetime.now().date()
            self.reschedule_task(task, datetime.combine(today, datetime.min.time()), failures)
        
        # Process regular tasks with one per day limit
        if regular_tasks:
            # Mark all regular tasks as failed and collect their data
            task_data = []
            for task in regular_tasks:
                failures = self.mark_failure(task)
                success_ratio = self.get_success_ratio(task)
                delay_hours = self.calculate_delay_hours(failures)
                task_data.append({
                    'task': task,
                    'failures': failures,
                    'success_ratio': success_ratio,
                    'delay_hours': delay_hours
                })
            
            # Sort by success ratio (highest first) for prioritization
            task_data.sort(key=lambda x: x['success_ratio'], reverse=True)
            
            # Schedule tasks with one per day, starting from today
            current_date = datetime.now().date()
            
            for i, data in enumerate(task_data):
                # Calculate the actual reschedule date
                min_delay_days = data['delay_hours'] // 24
                actual_delay_days = max(min_delay_days, i)  # Ensure at least i days for spreading
                
                reschedule_date = current_date + timedelta(days=actual_delay_days)
                
                self.reschedule_task(data['task'], datetime.combine(reschedule_date, datetime.min.time()), data['failures'])

    def track_completion(self, task):
        """Track successful completion of a task"""
        if not task.completed_at:
            return
            
        metadata = self.get_metadata(task)
        last_completion = metadata.get("last_completion")
        current_completion = task.completed_at.isoformat()
        
        # Check if this is a new completion
        if last_completion != current_completion:
            successes = metadata.get("successes", 0) + 1
            self.update_metadata(task.id, {
                "successes": successes,
                "last_completion": current_completion
            })
            print(f"Tracked completion: {task.content} (success #{successes})")


def main():
    manager = TodoistMetadataManager(os.environ.get('TODOIST_API_KEY'))

    # Get overdue tasks
    overdue_tasks = manager.filter_tasks_flattened(query="overdue")

    # Batch reschedule overdue tasks
    manager.batch_reschedule_overdue(overdue_tasks)
    print(f"Processed {len(overdue_tasks)} overdue tasks")
    
    # Track recently completed tasks
    yesterday = datetime.now() - timedelta(days=1)
    today = datetime.now()
    
    completed_tasks_iter = manager.api.get_completed_tasks_by_completion_date(since=yesterday, until=today)
    completed_tasks = []
    for task_list in completed_tasks_iter:
        completed_tasks.extend(task_list)
    
    for task in completed_tasks:
        print(task)
        manager.track_completion(task)
    
    print(f"Tracked {len(completed_tasks)} completed tasks")


if __name__ == "__main__":
    main()