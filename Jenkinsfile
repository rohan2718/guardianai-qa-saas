pipeline {
    agent any

    stages {

        stage('Build') {
            steps {
                bat 'echo Building the project'
            }
        }

        stage('Test') {
            steps {
                bat 'echo Running tests'
            }
        }

        stage('Deploy') {
            steps {
                bat 'echo Deploying application'
            }
        }
    }

    post {
        success {
            echo 'Pipeline completed successfully'
        }
        failure {
            echo 'Pipeline failed'
        }
    }
}
